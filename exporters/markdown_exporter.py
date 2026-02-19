"""
exporters/markdown_exporter.py
─────────────────────────────────────────────────────────────────────────────
MergedDocument의 요소 목록을 최종 Markdown 파일로 변환한다.

[변환 규칙]
  - heading     → # ~ #### (level 1~4)
  - paragraph   → 텍스트 그대로
  - table       → Markdown 표 (이미 변환됨)
  - list_item   → - 항목
  - caption     → *이탤릭* 처리
  - header/footer → 출력 제외 (페이지 번호, 머리글 등)

[후처리]
  - 연속 빈 줄 압축 (max_blank_lines 설정 기준)
  - heading 앞뒤 빈 줄 보장
  - 표 앞뒤 빈 줄 보장
  - 마지막 줄 개행 문자로 마무리
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from config import Config, DEFAULT_CONFIG
from processors.merger import DocumentElement, MergedDocument


# ─────────────────────────────────────────────
# 요소 → Markdown 라인 변환
# ─────────────────────────────────────────────

def _element_to_lines(elem: DocumentElement, cfg) -> list[str]:
    """
    DocumentElement 하나를 Markdown 라인 목록으로 변환한다.
    빈 줄 삽입은 이 함수에서 처리하지 않고 조합 단계에서 처리한다.
    """
    text = elem.text.strip()
    if not text:
        return []

    if elem.kind == "heading":
        level = elem.level or 1
        # heading 레벨이 범위를 벗어나면 클램프
        level = max(1, min(level, 6))
        prefix = "#" * level
        return [f"{prefix} {text}"]

    elif elem.kind == "table":
        # 이미 Markdown 표 형식으로 변환된 상태
        return text.splitlines()

    elif elem.kind == "list_item":
        # 이미 "- " 또는 "1. "으로 시작하면 그대로, 아니면 추가
        if re.match(r"^(\-|\*|\d+\.)\s", text):
            return [text]
        return [f"- {text}"]

    elif elem.kind == "caption":
        return [f"*{text}*"]

    elif elem.kind in ("header", "footer"):
        # 페이지 헤더/푸터는 출력하지 않음
        return []

    else:
        # paragraph
        return [text]


def _compress_blank_lines(lines: list[str], max_blanks: int) -> list[str]:
    """
    연속된 빈 줄을 max_blanks개로 압축한다.
    예: max_blanks=2이면 세 줄 이상 연속 빈 줄을 두 줄로 줄임.
    """
    result: list[str] = []
    blank_count = 0

    for line in lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= max_blanks:
                result.append("")
        else:
            blank_count = 0
            result.append(line)

    return result


# ─────────────────────────────────────────────
# 메인 익스포터
# ─────────────────────────────────────────────

class MarkdownExporter:
    """
    MergedDocument를 Markdown 문자열 또는 파일로 변환한다.

    사용법:
        exporter = MarkdownExporter(config)
        md_text = exporter.to_string(merged_doc)
        exporter.to_file(merged_doc, output_path)
    """

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg_output = config.output

    def to_string(self, doc: MergedDocument) -> str:
        """
        MergedDocument를 Markdown 문자열로 변환한다.

        Parameters
        ----------
        doc : MergedDocument
            병합된 문서 구조

        Returns
        -------
        str
            최종 Markdown 텍스트
        """
        lines: list[str] = []
        prev_kind: str | None = None

        for elem in doc.elements:
            # 페이지 헤더/푸터는 완전히 제외
            if elem.is_skippable:
                continue

            elem_lines = _element_to_lines(elem, self.cfg_output)
            if not elem_lines:
                continue

            # ── 앞 빈 줄 삽입 ─────────────────────
            needs_blank_before = False

            if self.cfg_output.heading_blank_lines and elem.kind == "heading":
                needs_blank_before = True
            elif self.cfg_output.table_blank_lines and elem.kind == "table":
                needs_blank_before = True
            elif prev_kind in ("heading", "table") and elem.kind not in ("heading", "table"):
                # 헤딩/표 뒤에 오는 요소 앞에도 빈 줄
                needs_blank_before = True

            if needs_blank_before and lines and lines[-1].strip() != "":
                lines.append("")

            # ── 요소 라인 추가 ────────────────────
            lines.extend(elem_lines)

            # ── 뒤 빈 줄 삽입 ─────────────────────
            needs_blank_after = False
            if self.cfg_output.heading_blank_lines and elem.kind == "heading":
                needs_blank_after = True
            elif self.cfg_output.table_blank_lines and elem.kind == "table":
                needs_blank_after = True

            if needs_blank_after:
                lines.append("")

            prev_kind = elem.kind

        # ── 후처리 ──────────────────────────────
        compressed = _compress_blank_lines(lines, self.cfg_output.max_blank_lines)

        # 앞뒤 불필요한 빈 줄 제거
        while compressed and compressed[0].strip() == "":
            compressed.pop(0)
        while compressed and compressed[-1].strip() == "":
            compressed.pop()

        # 마지막에 개행 문자 추가 (POSIX 표준)
        return "\n".join(compressed) + "\n"

    def to_file(
        self,
        doc: MergedDocument,
        output_path: str | Path | None = None,
    ) -> Path:
        """
        MergedDocument를 Markdown 파일로 저장한다.

        Parameters
        ----------
        doc : MergedDocument
            병합된 문서 구조
        output_path : str | Path | None
            저장 경로. None이면 config의 output_dir + PDF 파일명 + suffix.md 사용

        Returns
        -------
        Path
            저장된 파일 경로
        """
        if output_path is None:
            pdf_stem = Path(doc.pdf_path).stem
            out_dir = self.cfg_output.output_dir or Path(doc.pdf_path).parent
            output_path = out_dir / f"{pdf_stem}{self.cfg_output.suffix}.md"

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        md_text = self.to_string(doc)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        logger.info(f"Markdown 저장 완료: {output_path} ({len(md_text):,} 문자)")
        return output_path
