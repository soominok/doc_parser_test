"""
Markdown 문서 Chunking 모듈

RAG에서 chunking은 검색 품질에 직접적인 영향을 미치는 핵심 단계입니다.

전략: Heading-Aware Recursive Chunking
- 단순 고정 크기 분할이 아닌, Markdown의 heading 구조를 인식하여 분할
- heading을 chunk 경계로 우선 사용하여 의미 단위를 보존
- 표(table)는 가능한 한 하나의 chunk에 보존
- 각 chunk에 메타데이터(출처 파일, heading 경로)를 부여

계층 구조:
  Level 1: heading 기반 분할 (의미 단위 보존)
  Level 2: 문단 기반 분할 (heading 내 텍스트가 길면)
  Level 3: 문장 기반 분할 (최후 수단)
"""
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import CHUNK_CONFIG
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Chunk:
    """하나의 텍스트 청크를 나타내는 데이터 클래스."""
    content: str                          # 청크 텍스트 내용
    metadata: dict = field(default_factory=dict)  # 메타데이터

    @property
    def char_count(self) -> int:
        return len(self.content)


class MarkdownChunker:
    """
    Markdown 문서를 heading 구조를 인식하여 청크로 분할합니다.

    주요 특징:
    1. heading 경계에서 우선 분할 (의미 단위 보존)
    2. 각 chunk에 "heading 경로" 메타데이터 부여
       예: "제1장 총칙 > 제1절 목적 > 1.1 배경"
    3. 표(table)를 하나의 단위로 유지
    4. chunk_size 초과 시 recursive하게 더 작은 단위로 분할
    """

    def __init__(
        self,
        chunk_size: int = None,
        chunk_overlap: int = None,
        min_chunk_size: int = None,
    ):
        """
        Args:
            chunk_size: 한 청크의 최대 글자 수 (기본: 1000)
            chunk_overlap: 청크 간 겹침 글자 수 (기본: 200)
            min_chunk_size: 최소 청크 크기, 이보다 작으면 이전 청크에 병합 (기본: 100)
        """
        self.chunk_size = chunk_size or CHUNK_CONFIG["chunk_size"]
        self.chunk_overlap = chunk_overlap or CHUNK_CONFIG["chunk_overlap"]
        self.min_chunk_size = min_chunk_size or CHUNK_CONFIG["min_chunk_size"]

    def chunk_markdown(
        self,
        markdown_text: str,
        source_file: str = "",
    ) -> list[Chunk]:
        """
        Markdown 텍스트를 heading 구조 기반으로 청크 분할합니다.

        처리 순서:
        1. heading 기반으로 섹션 분할
        2. 각 섹션이 chunk_size 이내면 하나의 chunk
        3. 초과하면 문단 → 문장 순으로 재분할
        4. 각 chunk에 heading 경로 메타데이터 부여
        5. chunk_overlap 만큼 인접 chunk와 겹침 추가

        Args:
            markdown_text: heading 계층이 복원된 Markdown
            source_file: 원본 파일명 (메타데이터용)

        Returns:
            list[Chunk]: 청크 목록
        """
        # 1단계: heading 기반 섹션 분할
        sections = self._split_by_headings(markdown_text)

        # 2단계: 각 섹션을 chunk_size에 맞게 분할
        raw_chunks = []
        for section in sections:
            heading_path = section["heading_path"]
            content = section["content"]

            if len(content) <= self.chunk_size:
                # 크기 이내: 그대로 하나의 chunk
                raw_chunks.append({
                    "content": content,
                    "heading_path": heading_path,
                })
            else:
                # 크기 초과: 재분할
                sub_chunks = self._recursive_split(content)
                for sc in sub_chunks:
                    raw_chunks.append({
                        "content": sc,
                        "heading_path": heading_path,
                    })

        # 3단계: 너무 작은 chunk는 이전 chunk에 병합
        merged_chunks = self._merge_small_chunks(raw_chunks)

        # 4단계: overlap 추가 및 Chunk 객체 생성
        final_chunks = self._add_overlap_and_create_chunks(
            merged_chunks, source_file
        )

        logger.info(
            f"Chunking 완료: {len(final_chunks)}개 청크 생성 "
            f"(원본 {len(markdown_text)}자)"
        )

        return final_chunks

    def _split_by_headings(self, markdown_text: str) -> list[dict]:
        """
        Markdown을 heading 기준으로 섹션으로 분할합니다.

        각 섹션에 "heading 경로"를 부여합니다.
        예: "# 제1장 총칙" 하위의 "## 제1절" 하위의 텍스트 →
            heading_path = "제1장 총칙 > 제1절"

        Returns:
            list[dict]: {"heading_path": str, "content": str}
        """
        heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
        lines = markdown_text.split("\n")

        sections = []
        current_headings = {}  # 레벨 → heading 텍스트 매핑
        current_content_lines = []
        current_heading_path = ""

        for line in lines:
            match = heading_pattern.match(line)
            if match:
                # 이전 섹션 저장
                if current_content_lines:
                    content = "\n".join(current_content_lines).strip()
                    if content:
                        sections.append({
                            "heading_path": current_heading_path,
                            "content": content,
                        })

                # heading 경로 업데이트
                level = len(match.group(1))
                heading_text = match.group(2).strip()

                # 현재 레벨 설정, 하위 레벨 초기화
                current_headings[level] = heading_text
                for l in list(current_headings.keys()):
                    if l > level:
                        del current_headings[l]

                # heading 경로 생성: "H1 > H2 > H3"
                path_parts = []
                for l in sorted(current_headings.keys()):
                    path_parts.append(current_headings[l])
                current_heading_path = " > ".join(path_parts)

                # 새 섹션 시작 (heading 라인 자체도 content에 포함)
                current_content_lines = [line]
            else:
                current_content_lines.append(line)

        # 마지막 섹션 저장
        if current_content_lines:
            content = "\n".join(current_content_lines).strip()
            if content:
                sections.append({
                    "heading_path": current_heading_path,
                    "content": content,
                })

        return sections

    def _recursive_split(self, text: str) -> list[str]:
        """
        chunk_size를 초과하는 텍스트를 재귀적으로 분할합니다.

        분할 우선순위:
        1. 빈 줄(문단 경계) 기준 분할
        2. 문장 경계(. ! ?) 기준 분할
        3. 최후 수단: 고정 크기 분할

        Args:
            text: 분할할 텍스트

        Returns:
            list[str]: 분할된 텍스트 조각 목록
        """
        if len(text) <= self.chunk_size:
            return [text]

        # 1차: 빈 줄(문단 경계)로 분할
        paragraphs = re.split(r"\n\s*\n", text)
        if len(paragraphs) > 1:
            return self._merge_splits(paragraphs)

        # 2차: 문장 경계로 분할
        sentences = re.split(r"(?<=[.!?。])\s+", text)
        if len(sentences) > 1:
            return self._merge_splits(sentences)

        # 3차: 고정 크기 분할 (최후 수단)
        chunks = []
        for i in range(0, len(text), self.chunk_size):
            chunks.append(text[i : i + self.chunk_size])
        return chunks

    def _merge_splits(self, parts: list[str]) -> list[str]:
        """
        분할된 조각들을 chunk_size에 맞게 병합합니다.

        짧은 조각들을 이어붙여 chunk_size에 가까운 크기로 만듭니다.

        Args:
            parts: 분할된 텍스트 조각들

        Returns:
            적절한 크기로 병합된 텍스트 목록
        """
        merged = []
        current = ""

        for part in parts:
            part = part.strip()
            if not part:
                continue

            if len(current) + len(part) + 2 <= self.chunk_size:
                current = f"{current}\n\n{part}" if current else part
            else:
                if current:
                    merged.append(current)
                # part 자체가 chunk_size 초과면 재귀 분할
                if len(part) > self.chunk_size:
                    merged.extend(self._recursive_split(part))
                else:
                    current = part
                    continue
                current = ""

        if current:
            merged.append(current)

        return merged

    def _merge_small_chunks(self, chunks: list[dict]) -> list[dict]:
        """
        min_chunk_size보다 작은 chunk를 이전 chunk에 병합합니다.

        Args:
            chunks: 원시 chunk 목록

        Returns:
            병합 완료된 chunk 목록
        """
        if not chunks:
            return chunks

        merged = [chunks[0]]

        for chunk in chunks[1:]:
            if len(chunk["content"]) < self.min_chunk_size and merged:
                # 이전 chunk에 병합
                merged[-1]["content"] += "\n\n" + chunk["content"]
            else:
                merged.append(chunk)

        return merged

    def _add_overlap_and_create_chunks(
        self,
        raw_chunks: list[dict],
        source_file: str,
    ) -> list[Chunk]:
        """
        인접 chunk 간 overlap을 추가하고 최종 Chunk 객체를 생성합니다.

        overlap은 이전 chunk의 마지막 N글자를 현재 chunk 앞에 추가하여
        청크 경계에서의 문맥 손실을 방지합니다.

        Args:
            raw_chunks: 병합 완료된 원시 chunk 목록
            source_file: 원본 파일명

        Returns:
            최종 Chunk 객체 목록
        """
        final_chunks = []

        for i, raw in enumerate(raw_chunks):
            content = raw["content"]

            # overlap 추가: 이전 chunk의 마지막 부분을 현재 chunk 앞에 추가
            if i > 0 and self.chunk_overlap > 0:
                prev_content = raw_chunks[i - 1]["content"]
                overlap_text = prev_content[-self.chunk_overlap :]
                # 단어 경계에서 자르기 (단어 중간에서 자르지 않음)
                space_idx = overlap_text.find(" ")
                if space_idx > 0:
                    overlap_text = overlap_text[space_idx + 1 :]
                content = f"...{overlap_text}\n\n{content}"

            chunk = Chunk(
                content=content,
                metadata={
                    "source_file": source_file,
                    "heading_path": raw["heading_path"],
                    "chunk_index": i,
                    "total_chunks": len(raw_chunks),
                },
            )
            final_chunks.append(chunk)

        return final_chunks
