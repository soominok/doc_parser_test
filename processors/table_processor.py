"""
processors/table_processor.py
─────────────────────────────────────────────────────────────────────────────
여러 파서에서 추출된 표 데이터를 Markdown 표 형식으로 변환한다.

[우선순위]
  1. pdfplumber 표  → 경계선 기반 정확도 높음 (우선 사용)
  2. docling 표     → 복잡한 레이아웃(다단, 세로 텍스트)에서 보완
  3. LLM 재구성     → 셀 병합이 있어 두 파서 모두 실패한 경우 (선택적)

[Markdown 표 변환 규칙]
  - 첫 번째 행을 헤더로 사용
  - 헤더 구분선: |---|---|...|
  - 셀 내 줄바꿈: <br> 또는 공백으로 변환 (설정 가능)
  - None 셀(병합 셀의 계속): 빈 문자열로 처리
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from config import Config, DEFAULT_CONFIG
from parsers.pdfplumber_parser import RawTable


# ─────────────────────────────────────────────
# 데이터 모델
# ─────────────────────────────────────────────

@dataclass
class MarkdownTable:
    """Markdown으로 변환된 표 하나"""

    page_number: int
    table_index: int
    source: str                     # "pdfplumber" | "docling" | "llm"
    markdown: str                   # 최종 Markdown 문자열
    row_count: int
    col_count: int
    # 표 위치 (페이지 내 정렬에 사용)
    bbox: tuple[float, float, float, float] = field(default_factory=tuple)

    @property
    def y0(self) -> float:
        return self.bbox[1] if self.bbox else 0.0


# ─────────────────────────────────────────────
# 내부 변환 유틸
# ─────────────────────────────────────────────

def _sanitize_cell(cell: str | None, cfg) -> str:
    """
    셀 텍스트를 Markdown에 안전한 형태로 변환한다.
    - None → 빈 문자열
    - 줄바꿈 → <br> 또는 공백 (설정에 따라)
    - 파이프 문자(|) → 이스케이프
    """
    if cell is None:
        return cfg.empty_cell_placeholder

    text = str(cell).strip()

    # 셀 내 파이프 문자 이스케이프 (Markdown 표 구조 깨짐 방지)
    text = text.replace("|", "\\|")

    # 줄바꿈 처리
    if cfg.newline_to_br:
        text = text.replace("\n", "<br>")
    else:
        text = text.replace("\n", " ").replace("\r", " ")

    # 연속 공백 정리
    import re
    text = re.sub(r" {2,}", " ", text)

    return text


def _make_separator_row(col_count: int, alignment: str = "left") -> str:
    """
    Markdown 표 헤더-본문 구분선을 생성한다.
    예: |---|---|---|
    """
    align_map = {
        "left": ":---",
        "center": ":---:",
        "right": "---:",
    }
    sep = align_map.get(alignment, ":---")
    return "| " + " | ".join([sep] * col_count) + " |"


def rows_to_markdown(
    rows: list[list[str | None]],
    cfg,
) -> str:
    """
    2D 셀 목록(rows)을 Markdown 표 문자열로 변환한다.

    Parameters
    ----------
    rows : list[list[str | None]]
        표 데이터 (rows[0]이 헤더 행)
    cfg : TableConfig
        표 관련 설정

    Returns
    -------
    str
        Markdown 표 문자열
    """
    if not rows:
        return ""

    # 모든 행의 열 수를 최대값으로 통일 (들쭉날쭉한 표 대응)
    max_cols = max(len(row) for row in rows)
    if max_cols < cfg.min_cols:
        return ""  # 열 수가 너무 적으면 표로 처리하지 않음

    md_lines: list[str] = []

    for i, row in enumerate(rows):
        # 열 수가 부족한 행은 빈 셀로 채움
        padded = list(row) + [None] * (max_cols - len(row))
        cells = [_sanitize_cell(cell, cfg) for cell in padded]
        md_lines.append("| " + " | ".join(cells) + " |")

        # 첫 번째 행(헤더) 뒤에 구분선 삽입
        if i == 0:
            md_lines.append(_make_separator_row(max_cols, cfg.default_alignment))

    return "\n".join(md_lines)


# ─────────────────────────────────────────────
# 메인 프로세서
# ─────────────────────────────────────────────

class TableProcessor:
    """
    pdfplumber 및 docling의 표 데이터를 수집하고
    Markdown 표로 변환한다.
    """

    def __init__(self, config: Config = DEFAULT_CONFIG):
        self.cfg = config.table

    def process_pdfplumber_tables(
        self,
        tables_by_page: dict[int, list[RawTable]],
    ) -> dict[int, list[MarkdownTable]]:
        """
        pdfplumber에서 추출한 페이지별 표를 Markdown으로 변환한다.

        Parameters
        ----------
        tables_by_page : dict[int, list[RawTable]]
            PdfPlumberResult.tables 필드

        Returns
        -------
        dict[int, list[MarkdownTable]]
            페이지 번호 → MarkdownTable 목록
        """
        result: dict[int, list[MarkdownTable]] = {}

        for page_num, raw_tables in tables_by_page.items():
            md_tables: list[MarkdownTable] = []

            for raw in raw_tables:
                if raw.row_count < self.cfg.min_rows:
                    continue

                markdown = rows_to_markdown(raw.rows, self.cfg)
                if not markdown:
                    continue

                md_tables.append(MarkdownTable(
                    page_number=page_num,
                    table_index=raw.table_index,
                    source="pdfplumber",
                    markdown=markdown,
                    row_count=raw.row_count,
                    col_count=raw.col_count,
                    bbox=raw.bbox,
                ))

            result[page_num] = md_tables

        return result

    def process_docling_tables(
        self,
        docling_elements,
    ) -> dict[int, list[MarkdownTable]]:
        """
        docling에서 추출한 표 요소를 Markdown으로 변환한다.
        pdfplumber가 실패한 표를 보완하는 용도로 사용한다.

        Parameters
        ----------
        docling_elements : list[DoclingElement]
            DoclingResult.elements 필드

        Returns
        -------
        dict[int, list[MarkdownTable]]
            페이지 번호 → MarkdownTable 목록
        """
        result: dict[int, list[MarkdownTable]] = {}
        table_idx_by_page: dict[int, int] = {}

        for elem in docling_elements:
            if not elem.is_table:
                continue

            page_num = elem.page_number

            # docling이 표 데이터를 제공한 경우
            if elem.table_data:
                markdown = rows_to_markdown(elem.table_data, self.cfg)
            elif elem.text:
                # docling이 Markdown으로 내보낸 경우 그대로 사용
                markdown = elem.text
            else:
                continue

            if not markdown:
                continue

            if page_num not in result:
                result[page_num] = []
                table_idx_by_page[page_num] = 0

            idx = table_idx_by_page[page_num]
            row_count = elem.table_data and len(elem.table_data) or 0
            col_count = (
                max(len(r) for r in elem.table_data) if elem.table_data else 0
            )

            result[page_num].append(MarkdownTable(
                page_number=page_num,
                table_index=idx,
                source="docling",
                markdown=markdown,
                row_count=row_count,
                col_count=col_count,
                bbox=elem.bbox,
            ))
            table_idx_by_page[page_num] = idx + 1

        return result

    def merge_tables(
        self,
        pdfplumber_tables: dict[int, list[MarkdownTable]],
        docling_tables: dict[int, list[MarkdownTable]],
    ) -> dict[int, list[MarkdownTable]]:
        """
        pdfplumber 표를 우선 사용하고,
        해당 페이지에 pdfplumber 표가 없으면 docling 표를 사용한다.

        병합 전략:
          - 같은 페이지에 pdfplumber 결과가 있으면 → pdfplumber 채택
          - pdfplumber 결과가 없으면 → docling 결과 채택
        """
        all_page_nums = set(pdfplumber_tables) | set(docling_tables)
        merged: dict[int, list[MarkdownTable]] = {}

        for page_num in all_page_nums:
            plumber = pdfplumber_tables.get(page_num, [])
            docling = docling_tables.get(page_num, [])

            if plumber:
                merged[page_num] = plumber
            elif docling:
                merged[page_num] = docling
            # 둘 다 없으면 제외

        return merged
