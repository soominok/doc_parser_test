"""
parsers/pdfplumber_parser.py
─────────────────────────────────────────────────────────────────────────────
pdfplumber를 사용하여 정확한 텍스트와 표(Table)를 추출한다.

[역할]
  - 페이지 좌표 기반의 정밀한 텍스트 추출 (잘림/순서 뒤바뀜 최소화)
  - 선 기반(선형 경계선) 표 감지 및 셀 단위 텍스트 추출
  - 표 영역을 제외한 본문 텍스트를 별도로 추출하여 병합 충돌 방지

[왜 pdfplumber인가?]
  - pdfminer 기반이라 텍스트 스트림 처리가 정확하다.
  - 표 감지 알고리즘이 내장되어 있어 셀 병합이 없는 일반 표에서 탁월하다.
  - 단점: heading 구조 인식 불가 → PyMuPDF·docling으로 보완
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber
import pdfplumber.page

from config import Config, DEFAULT_CONFIG


# ─────────────────────────────────────────────
# 데이터 모델
# ─────────────────────────────────────────────

@dataclass
class RawTable:
    """pdfplumber가 감지한 표 하나의 데이터"""

    page_number: int                      # 0-based 페이지 번호
    table_index: int                      # 페이지 내 표 순번
    bbox: tuple[float, float, float, float]  # 표 영역 (x0, y0, x1, y1)

    # 2D 리스트: rows[row_idx][col_idx] = 셀 텍스트 (None이면 병합 셀)
    rows: list[list[str | None]] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def col_count(self) -> int:
        return max((len(r) for r in self.rows), default=0)


@dataclass
class TextBlock:
    """표 영역을 제외한 일반 텍스트 블록"""

    page_number: int
    text: str
    bbox: tuple[float, float, float, float]

    @property
    def y0(self) -> float:
        return self.bbox[1]


@dataclass
class PdfPlumberResult:
    """pdfplumber 파싱 결과 전체"""

    pdf_path: str
    page_count: int
    # 페이지 번호(0-based) → 텍스트 블록 목록
    text_blocks: dict[int, list[TextBlock]] = field(default_factory=dict)
    # 페이지 번호(0-based) → 표 목록
    tables: dict[int, list[RawTable]] = field(default_factory=dict)


# ─────────────────────────────────────────────
# 내부 유틸
# ─────────────────────────────────────────────

def _bbox_overlap(
    b1: tuple[float, float, float, float],
    b2: tuple[float, float, float, float],
    threshold: float = 0.5,
) -> bool:
    """
    두 bbox가 threshold 비율 이상 겹치는지 판단한다.
    표 영역과 텍스트 블록 영역이 겹치는지 확인하여
    표 내부 텍스트를 일반 텍스트 목록에서 제외하는 데 사용한다.
    """
    ix0 = max(b1[0], b2[0])
    iy0 = max(b1[1], b2[1])
    ix1 = min(b1[2], b2[2])
    iy1 = min(b1[3], b2[3])

    if ix1 <= ix0 or iy1 <= iy0:
        return False

    inter_area = (ix1 - ix0) * (iy1 - iy0)
    b1_area = (b1[2] - b1[0]) * (b1[3] - b1[1])
    if b1_area == 0:
        return False

    return (inter_area / b1_area) >= threshold


def _extract_tables_from_page(
    page: pdfplumber.page.Page,
    page_num: int,
    cfg_table,
) -> list[RawTable]:
    """
    한 페이지에서 표를 감지하고 RawTable 목록으로 반환한다.

    pdfplumber의 find_tables()는 선 기반 경계선으로 표를 감지한다.
    snap_tolerance, join_tolerance 값으로 경계선 허용 오차를 조절한다.
    """
    table_settings = {
        "vertical_strategy": "lines",     # 수직 경계: 실제 선 사용
        "horizontal_strategy": "lines",   # 수평 경계: 실제 선 사용
        "snap_tolerance": cfg_table.snap_tolerance,
        "join_tolerance": cfg_table.join_tolerance,
        "edge_min_length": cfg_table.edge_min_length,
        "min_words_vertical": 1,
        "min_words_horizontal": 1,
    }

    raw_tables: list[RawTable] = []

    try:
        found = page.find_tables(table_settings=table_settings)
    except Exception:
        # 선이 없는 페이지에서도 오류 없이 통과
        return []

    for t_idx, tbl in enumerate(found):
        try:
            rows_raw = tbl.extract(x_tolerance=3, y_tolerance=3)
        except Exception:
            continue

        if not rows_raw:
            continue

        # 빈 행 제거
        rows_clean: list[list[str | None]] = []
        for row in rows_raw:
            cleaned = [
                (cell.strip() if isinstance(cell, str) else cell)
                for cell in row
            ]
            # 모든 셀이 비어 있는 행은 제외
            if any(c for c in cleaned if c):
                rows_clean.append(cleaned)

        if len(rows_clean) < cfg_table.min_rows:
            continue

        if rows_clean and len(rows_clean[0]) < cfg_table.min_cols:
            continue

        raw_tables.append(RawTable(
            page_number=page_num,
            table_index=t_idx,
            bbox=tbl.bbox,
            rows=rows_clean,
        ))

    return raw_tables


def _extract_text_blocks_excluding_tables(
    page: pdfplumber.page.Page,
    page_num: int,
    table_bboxes: list[tuple[float, float, float, float]],
) -> list[TextBlock]:
    """
    표 영역을 제외한 나머지 영역에서 텍스트를 블록 단위로 추출한다.

    pdfplumber의 chars를 직접 다루어 좌표 기반으로 표 외부 텍스트만 선택한다.
    """
    # 표 영역에 속하는 문자를 제외
    filtered_chars = []
    for char in page.chars:
        char_bbox = (char["x0"], char["top"], char["x1"], char["bottom"])
        in_table = any(
            _bbox_overlap(char_bbox, t_bbox, threshold=0.3)
            for t_bbox in table_bboxes
        )
        if not in_table:
            filtered_chars.append(char)

    if not filtered_chars:
        return []

    # 필터링된 문자들로만 구성된 임시 페이지 객체를 만들어 텍스트 추출
    # pdfplumber의 within_bbox + crop 대신 문자 묶음으로 줄 구성
    blocks: list[TextBlock] = []
    current_line_chars: list[dict] = []
    current_top: float | None = None
    line_tolerance = 5.0  # 같은 줄로 묶을 y 좌표 허용 오차

    for char in sorted(filtered_chars, key=lambda c: (round(c["top"] / line_tolerance), c["x0"])):
        top = char["top"]
        if current_top is None:
            current_top = top

        if abs(top - current_top) <= line_tolerance:
            current_line_chars.append(char)
        else:
            # 새 줄 시작 → 현재 줄 저장
            if current_line_chars:
                line_text = "".join(c["text"] for c in current_line_chars)
                if line_text.strip():
                    x0 = min(c["x0"] for c in current_line_chars)
                    y0 = min(c["top"] for c in current_line_chars)
                    x1 = max(c["x1"] for c in current_line_chars)
                    y1 = max(c["bottom"] for c in current_line_chars)
                    blocks.append(TextBlock(
                        page_number=page_num,
                        text=line_text,
                        bbox=(x0, y0, x1, y1),
                    ))
            current_line_chars = [char]
            current_top = top

    # 마지막 줄 저장
    if current_line_chars:
        line_text = "".join(c["text"] for c in current_line_chars)
        if line_text.strip():
            x0 = min(c["x0"] for c in current_line_chars)
            y0 = min(c["top"] for c in current_line_chars)
            x1 = max(c["x1"] for c in current_line_chars)
            y1 = max(c["bottom"] for c in current_line_chars)
            blocks.append(TextBlock(
                page_number=page_num,
                text=line_text,
                bbox=(x0, y0, x1, y1),
            ))

    return blocks


# ─────────────────────────────────────────────
# 메인 파서
# ─────────────────────────────────────────────

def parse(pdf_path: str | Path, config: Config = DEFAULT_CONFIG) -> PdfPlumberResult:
    """
    PDF 파일 전체에서 표와 본문 텍스트를 추출한다.

    Parameters
    ----------
    pdf_path : str | Path
        파싱할 PDF 파일 경로
    config : Config
        전역 설정 객체

    Returns
    -------
    PdfPlumberResult
        페이지별 텍스트 블록 + 표 목록
    """
    pdf_path = Path(pdf_path)
    result = PdfPlumberResult(
        pdf_path=str(pdf_path),
        page_count=0,
    )

    with pdfplumber.open(str(pdf_path)) as pdf:
        result.page_count = len(pdf.pages)
        max_pages = config.parser.max_pages
        pages_to_process = (
            pdf.pages[:max_pages] if max_pages else pdf.pages
        )

        for page_num, page in enumerate(pages_to_process):
            # 1) 표 추출
            tables = _extract_tables_from_page(page, page_num, config.table)
            result.tables[page_num] = tables

            # 2) 표 bbox 목록
            table_bboxes = [t.bbox for t in tables]

            # 3) 표 영역을 제외한 텍스트 추출
            text_blocks = _extract_text_blocks_excluding_tables(
                page, page_num, table_bboxes
            )
            result.text_blocks[page_num] = text_blocks

    return result
