"""
Heading 계층 구조 복원 모듈

문제:
- Docling은 heading을 인식하지만, ## (H2) 단일 레벨로 출력하는 경우가 많음
- 실제 문서는 제1장 > 제1절 > 1.1 > 1.1.1 등 다양한 계층 구조를 가짐
- PDF 양식에 따라 heading 구분 방식이 매우 다름

해결 전략 (Multi-Strategy Approach):
1. [전략 1] 번호 패턴 기반: "제1장", "1.1", "(1)" 등 번호 체계로 heading 레벨 판단
2. [전략 2] 폰트 크기 기반: PyMuPDF 메타데이터의 폰트 크기 비율로 판단
3. [전략 3] 복합 판단: 전략 1 + 2를 결합, 신뢰도가 높은 쪽을 우선 적용

처리 흐름:
  Docling Markdown (## 단일 레벨)
      ↓
  PyMuPDF 폰트 메타데이터
      ↓
  전략 1: 번호 패턴 매칭
  전략 2: 폰트 크기 비율 분석
      ↓
  복합 판단 → heading 레벨 재할당
      ↓
  #, ##, ###, ####, ##### 로 변환된 Markdown
"""
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import HEADING_CONFIG
from src.utils.logger import get_logger

logger = get_logger(__name__)


class HeadingProcessor:
    """
    Markdown의 heading 계층을 복원하는 프로세서.

    다중 전략(번호 패턴 + 폰트 크기)을 사용하여
    Docling이 ## 단일 레벨로 출력한 heading을
    적절한 계층(H1~H5)으로 재할당합니다.
    """

    def __init__(self, heading_config: dict = None):
        """
        Args:
            heading_config: heading 판단 설정
                (None이면 config/settings.py의 HEADING_CONFIG 사용)
        """
        self.config = heading_config or HEADING_CONFIG
        # 번호 패턴 컴파일 (성능 최적화)
        self._compiled_patterns = self._compile_patterns()

    def _compile_patterns(self) -> dict[str, list[re.Pattern]]:
        """설정의 번호 패턴을 정규식으로 미리 컴파일합니다."""
        compiled = {}
        for level, patterns in self.config["numbering_patterns"].items():
            compiled[level] = [re.compile(p) for p in patterns]
        return compiled

    # ================================================================
    # 전략 1: 번호 패턴 기반 heading 레벨 판단
    # ================================================================
    def detect_level_by_numbering(self, heading_text: str) -> str | None:
        """
        heading 텍스트의 번호 패턴을 분석하여 레벨을 반환합니다.

        예시:
            "제1장 총칙" → "h1"
            "1. 개요" → "h2"
            "1.1 배경" → "h3"
            "1.1.1 세부 항목" → "h4"

        Args:
            heading_text: heading 텍스트 (# 마크 제거 후)

        Returns:
            "h1"~"h5" 또는 None (패턴 매칭 실패 시)
        """
        text = heading_text.strip()

        # 각 레벨의 패턴을 순서대로 검사
        # 더 구체적인 패턴(h5 → h4 → h3 → h2 → h1)부터 검사하여
        # "1.1.1"이 "1."에 먼저 매칭되는 것을 방지
        for level in ["h5", "h4", "h3", "h2", "h1"]:
            if level not in self._compiled_patterns:
                continue
            for pattern in self._compiled_patterns[level]:
                if pattern.match(text):
                    return level

        return None

    # ================================================================
    # 전략 2: 폰트 크기 기반 heading 레벨 판단
    # ================================================================
    def detect_level_by_font_size(
        self,
        heading_text: str,
        text_blocks: list[dict],
        font_stats: dict,
    ) -> str | None:
        """
        PyMuPDF의 폰트 메타데이터로 heading 레벨을 판단합니다.

        판단 기준:
        - 본문 폰트 크기 대비 비율로 heading 레벨 결정
        - 본문의 1.8배 이상 → H1
        - 본문의 1.4배 이상 → H2
        - 본문의 1.15배 이상 → H3
        - 본문과 같지만 bold → H4

        Args:
            heading_text: heading 텍스트
            text_blocks: PyMuPDF에서 추출한 텍스트 블록 메타데이터
            font_stats: 폰트 통계 정보 (body_font_size 포함)

        Returns:
            "h1"~"h4" 또는 None
        """
        body_size = font_stats.get("body_font_size", 10.0)
        ratios = self.config["font_size_ratios"]

        # heading 텍스트와 매칭되는 텍스트 블록 찾기
        matching_block = self._find_matching_block(heading_text, text_blocks)
        if not matching_block:
            return None

        font_size = matching_block["font_size"]
        is_bold = matching_block["is_bold"]
        ratio = font_size / body_size if body_size > 0 else 1.0

        # 비율 기반 레벨 판단
        if ratio >= ratios["h1"]:
            return "h1"
        elif ratio >= ratios["h2"]:
            return "h2"
        elif ratio >= ratios["h3"]:
            return "h3"
        elif is_bold and ratio >= ratios["h4"]:
            return "h4"

        return None

    def _find_matching_block(
        self, heading_text: str, text_blocks: list[dict]
    ) -> dict | None:
        """
        heading 텍스트와 일치하는 PyMuPDF 텍스트 블록을 찾습니다.

        정확 일치 → 부분 일치 → 유사도 기반 순으로 검색합니다.

        Args:
            heading_text: 찾을 heading 텍스트
            text_blocks: PyMuPDF 텍스트 블록 목록

        Returns:
            매칭된 텍스트 블록 또는 None
        """
        clean_heading = heading_text.strip()

        # 1차: 정확 일치
        for block in text_blocks:
            if block["text"].strip() == clean_heading:
                return block

        # 2차: 부분 일치 (heading이 블록 텍스트에 포함)
        for block in text_blocks:
            block_text = block["text"].strip()
            if len(clean_heading) >= 3 and clean_heading in block_text:
                return block
            if len(block_text) >= 3 and block_text in clean_heading:
                return block

        # 3차: 공백/특수문자 제거 후 비교
        def normalize(s):
            return re.sub(r"\s+", "", s).strip()

        norm_heading = normalize(clean_heading)
        for block in text_blocks:
            if normalize(block["text"]) == norm_heading:
                return block

        return None

    # ================================================================
    # 전략 3: 복합 판단 (전략 1 + 2 결합)
    # ================================================================
    def determine_heading_level(
        self,
        heading_text: str,
        text_blocks: list[dict] = None,
        font_stats: dict = None,
    ) -> str:
        """
        번호 패턴과 폰트 크기를 복합적으로 분석하여
        최종 heading 레벨을 결정합니다.

        우선순위:
        1. 번호 패턴이 명확하면 → 패턴 결과 우선
        2. 번호 패턴이 없고 폰트 정보가 있으면 → 폰트 결과 사용
        3. 둘 다 없으면 → 기본 h2 유지

        Args:
            heading_text: heading 텍스트
            text_blocks: PyMuPDF 텍스트 블록 (선택)
            font_stats: 폰트 통계 (선택)

        Returns:
            "h1"~"h5" 레벨 문자열
        """
        # 전략 1: 번호 패턴 분석
        pattern_level = self.detect_level_by_numbering(heading_text)

        # 전략 2: 폰트 크기 분석 (메타데이터가 있을 때만)
        font_level = None
        if text_blocks and font_stats:
            font_level = self.detect_level_by_font_size(
                heading_text, text_blocks, font_stats
            )

        # 복합 판단
        if pattern_level and font_level:
            # 둘 다 있으면: 번호 패턴 우선 (더 명확한 구분)
            # 단, 폰트 크기와 2단계 이상 차이나면 폰트 크기 결과를 참고
            p_num = int(pattern_level[1])
            f_num = int(font_level[1])
            if abs(p_num - f_num) >= 2:
                # 큰 차이 → 둘 중 더 높은(작은 숫자) 레벨 선택
                final_level = f"h{min(p_num, f_num)}"
                logger.debug(
                    f"heading 레벨 충돌: 패턴={pattern_level}, 폰트={font_level} "
                    f"→ {final_level} (보수적 선택)"
                )
                return final_level
            return pattern_level  # 번호 패턴 우선
        elif pattern_level:
            return pattern_level
        elif font_level:
            return font_level
        else:
            return "h2"  # 기본값

    # ================================================================
    # Markdown 전체 처리
    # ================================================================
    def process_markdown(
        self,
        markdown_text: str,
        text_blocks: list[dict] = None,
        font_stats: dict = None,
    ) -> str:
        """
        Markdown 전체에서 heading을 찾아 계층 구조를 재할당합니다.

        처리 과정:
        1. Markdown에서 heading 라인(# 으로 시작)을 모두 추출
        2. 각 heading의 텍스트를 분석하여 적절한 레벨 결정
        3. # 개수를 레벨에 맞게 교체

        Args:
            markdown_text: Docling이 생성한 원본 Markdown
            text_blocks: PyMuPDF 텍스트 블록 메타데이터 (선택)
            font_stats: 폰트 통계 (선택)

        Returns:
            heading 계층이 복원된 Markdown 문자열
        """
        lines = markdown_text.split("\n")
        processed_lines = []

        # heading 패턴: 1개 이상의 # + 공백 + 텍스트
        heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

        heading_count = 0
        level_distribution = Counter()

        for line in lines:
            match = heading_pattern.match(line)
            if not match:
                processed_lines.append(line)
                continue

            original_hashes = match.group(1)
            heading_text = match.group(2).strip()

            # heading 레벨 결정
            new_level = self.determine_heading_level(
                heading_text, text_blocks, font_stats
            )

            # 레벨에 해당하는 # 개수
            level_num = int(new_level[1])
            new_hashes = "#" * level_num

            processed_lines.append(f"{new_hashes} {heading_text}")

            heading_count += 1
            level_distribution[new_level] += 1

        # 결과 로깅
        if heading_count > 0:
            logger.info(
                f"Heading 계층 복원 완료: {heading_count}개 heading 처리\n"
                f"  레벨 분포: {dict(level_distribution)}"
            )

        return "\n".join(processed_lines)

    def process_markdown_with_context_analysis(
        self,
        markdown_text: str,
        text_blocks: list[dict] = None,
        font_stats: dict = None,
    ) -> str:
        """
        문맥을 고려한 heading 계층 복원 (고급).

        단순 패턴 매칭 외에 문서 전체의 heading 구조를 분석하여
        일관성 있는 계층 구조를 보장합니다.

        규칙:
        - 같은 번호 패턴의 heading은 같은 레벨이어야 함
        - heading 레벨은 한 번에 1단계씩만 깊어져야 함 (H1 → H3 불가, H1 → H2 → H3 필요)
        - 이전 heading보다 갑자기 얕아지면 섹션 전환으로 판단

        Args:
            markdown_text: 원본 Markdown
            text_blocks: PyMuPDF 메타데이터 (선택)
            font_stats: 폰트 통계 (선택)

        Returns:
            문맥 분석을 반영한 Markdown
        """
        lines = markdown_text.split("\n")
        heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

        # 1단계: 모든 heading을 수집하고 레벨 결정
        headings = []
        for i, line in enumerate(lines):
            match = heading_pattern.match(line)
            if match:
                heading_text = match.group(2).strip()
                raw_level = self.determine_heading_level(
                    heading_text, text_blocks, font_stats
                )
                headings.append({
                    "line_index": i,
                    "text": heading_text,
                    "raw_level": raw_level,
                    "level_num": int(raw_level[1]),
                })

        if not headings:
            return markdown_text

        # 2단계: 연속성 보정 — heading 레벨이 2단계 이상 갑자기 깊어지면 보정
        corrected_headings = self._correct_heading_continuity(headings)

        # 3단계: 보정된 레벨을 Markdown에 적용
        processed_lines = lines.copy()
        for h in corrected_headings:
            new_hashes = "#" * h["corrected_level"]
            processed_lines[h["line_index"]] = f"{new_hashes} {h['text']}"

        level_dist = Counter(h["corrected_level"] for h in corrected_headings)
        logger.info(
            f"Heading 계층 복원 완료 (문맥 분석): {len(corrected_headings)}개\n"
            f"  레벨 분포: {dict(level_dist)}"
        )

        return "\n".join(processed_lines)

    def _correct_heading_continuity(self, headings: list[dict]) -> list[dict]:
        """
        heading 레벨의 연속성을 보정합니다.

        규칙:
        - 첫 heading은 그대로 유지
        - 이전 heading보다 2단계 이상 깊어지면, 이전 heading + 1로 보정
        - 이전보다 얕아지는 것은 섹션 전환이므로 허용

        예시:
            H1 → H4 (2단계 건너뜀) → H1 → H2 로 보정

        Args:
            headings: 원시 heading 목록

        Returns:
            corrected_level 필드가 추가된 heading 목록
        """
        if not headings:
            return headings

        # 첫 heading은 원본 레벨 유지
        headings[0]["corrected_level"] = headings[0]["level_num"]

        for i in range(1, len(headings)):
            current_level = headings[i]["level_num"]
            prev_corrected = headings[i - 1]["corrected_level"]

            if current_level > prev_corrected + 1:
                # 2단계 이상 깊어짐 → 이전 + 1로 보정
                corrected = prev_corrected + 1
                logger.debug(
                    f"heading 연속성 보정: '{headings[i]['text'][:30]}...' "
                    f"H{current_level} → H{corrected}"
                )
                headings[i]["corrected_level"] = corrected
            else:
                headings[i]["corrected_level"] = current_level

        return headings
