"""
parsers/docling_parser.py
─────────────────────────────────────────────────────────────────────────────
Docling을 사용하여 PDF 문서의 구조(레이아웃)를 추출한다.

[역할]
  - Heading 계층(H1/H2/H3...), 단락, 표, 목록, 캡션 등 문서 요소 분류
  - 딥러닝 기반 레이아웃 인식이므로 다양한 문서 양식에 강인하다.
  - 텍스트 자체의 정확도보다 '이 블록이 무엇인가(레이블)'가 핵심 출력

[docling의 한계 → 보완 전략]
  - 텍스트 잘림/순서 역전: PyMuPDF·pdfplumber 텍스트로 대체
  - 헤딩 계층(H1 vs H2) 부정확 시: 폰트 크기 기반 HeadingDetector로 보정
  - 표 내용 불정확 시: pdfplumber 표로 대체

[반환 구조]
  DoclingResult
  └── elements: list[DoclingElement]
        - label: "title" | "section_header" | "text" | "table" |
                 "list_item" | "caption" | "page_header" | "page_footer"
        - level: heading 레벨 (None이면 heading 아님)
        - text: docling이 추출한 텍스트
        - bbox: 페이지 내 좌표
        - page_number: 0-based
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from config import Config, DEFAULT_CONFIG

# Docling label 상수 (docling >= 2.x 기준)
LABEL_TITLE = "title"
LABEL_SECTION_HEADER = "section_header"
LABEL_TEXT = "text"
LABEL_TABLE = "table"
LABEL_LIST_ITEM = "list_item"
LABEL_CAPTION = "caption"
LABEL_PAGE_HEADER = "page_header"
LABEL_PAGE_FOOTER = "page_footer"
LABEL_FIGURE = "figure"
LABEL_FORMULA = "formula"
LABEL_CODE = "code"

# heading으로 간주할 레이블 집합
HEADING_LABELS = {LABEL_TITLE, LABEL_SECTION_HEADER}

DocElementLabel = Literal[
    "title", "section_header", "text", "table",
    "list_item", "caption", "page_header", "page_footer",
    "figure", "formula", "code", "unknown"
]


# ─────────────────────────────────────────────
# 데이터 모델
# ─────────────────────────────────────────────

@dataclass
class DoclingElement:
    """Docling이 추출한 문서 요소 하나"""

    label: DocElementLabel            # 요소 유형 레이블
    text: str                         # docling이 인식한 텍스트 (정확도 낮을 수 있음)
    page_number: int                  # 0-based 페이지 번호
    bbox: tuple[float, float, float, float]   # 좌표 (x0, y0, x1, y1) in pt
    level: int | None = None          # heading 레벨 (None이면 본문 요소)
    element_index: int = 0            # 문서 내 순번 (읽기 순서)

    # 표 요소일 때 셀 데이터 (rows[row][col] = str)
    table_data: list[list[str]] | None = None

    @property
    def is_heading(self) -> bool:
        return self.label in HEADING_LABELS

    @property
    def is_table(self) -> bool:
        return self.label == LABEL_TABLE

    @property
    def y0(self) -> float:
        return self.bbox[1]


@dataclass
class DoclingResult:
    """Docling 파싱 결과 전체"""

    pdf_path: str
    page_count: int
    # 문서 읽기 순서대로 정렬된 요소 목록
    elements: list[DoclingElement] = field(default_factory=list)
    # 페이지 번호(0-based) → 해당 페이지 요소 목록
    pages: dict[int, list[DoclingElement]] = field(default_factory=dict)


# ─────────────────────────────────────────────
# 내부 변환 헬퍼
# ─────────────────────────────────────────────

def _normalize_label(raw_label: str) -> DocElementLabel:
    """
    docling의 내부 레이블을 표준화된 레이블로 변환한다.
    docling 버전마다 레이블 이름이 다를 수 있어 매핑 테이블로 처리한다.
    """
    label_map: dict[str, DocElementLabel] = {
        # docling v2 기준
        "title": "title",
        "section_header": "section_header",
        "SectionHeaderItem": "section_header",
        "text": "text",
        "TextItem": "text",
        "paragraph": "text",
        "table": "table",
        "TableItem": "table",
        "list_item": "list_item",
        "ListItem": "list_item",
        "caption": "caption",
        "CaptionItem": "caption",
        "page_header": "page_header",
        "PageHeaderItem": "page_header",
        "page_footer": "page_footer",
        "PageFooterItem": "page_footer",
        "figure": "figure",
        "FigureItem": "figure",
        "formula": "formula",
        "FormulaItem": "formula",
        "code": "code",
        "CodeItem": "code",
    }
    return label_map.get(raw_label, "unknown")


def _extract_heading_level(element_data: dict[str, Any]) -> int | None:
    """
    docling 요소에서 heading 레벨을 추출한다.
    docling은 section_header에 'level' 속성을 제공하기도 한다.
    없으면 None을 반환하고, HeadingDetector가 폰트 크기로 결정한다.
    """
    # docling v2: element.level 또는 prov[].level
    level = element_data.get("level")
    if level is not None and isinstance(level, int):
        return max(1, min(level, 6))  # 1~6 범위로 클램프

    # 일부 버전에서는 "heading_level" 키로 제공
    heading_level = element_data.get("heading_level")
    if heading_level is not None:
        return max(1, min(int(heading_level), 6))

    return None


def _parse_table_data(table_element: Any) -> list[list[str]] | None:
    """
    docling 표 요소에서 셀 데이터를 2D 리스트로 추출한다.
    docling은 TableItem 내부에 grid 또는 data 형태로 셀을 제공한다.
    """
    try:
        # docling v2: table.data.grid
        if hasattr(table_element, "data") and hasattr(table_element.data, "grid"):
            grid = table_element.data.grid
            rows = []
            for row in grid:
                cells = []
                for cell in row:
                    # cell.text 또는 cell.body
                    text = ""
                    if hasattr(cell, "text"):
                        text = str(cell.text or "")
                    elif hasattr(cell, "body"):
                        text = str(cell.body or "")
                    cells.append(text.strip())
                rows.append(cells)
            return rows if rows else None
    except Exception:
        pass
    return None


def _element_to_bbox(prov_list: list[dict]) -> tuple[float, float, float, float]:
    """
    docling의 prov(provenance) 목록에서 bbox를 추출한다.
    여러 prov가 있으면 첫 번째 것을 사용한다.
    """
    if not prov_list:
        return (0.0, 0.0, 0.0, 0.0)
    prov = prov_list[0]
    bbox_data = prov.get("bbox", {})
    if isinstance(bbox_data, dict):
        return (
            float(bbox_data.get("l", 0)),
            float(bbox_data.get("t", 0)),
            float(bbox_data.get("r", 0)),
            float(bbox_data.get("b", 0)),
        )
    return (0.0, 0.0, 0.0, 0.0)


def _get_page_number(prov_list: list[dict]) -> int:
    """prov 목록에서 페이지 번호를 추출 (0-based로 변환)"""
    if not prov_list:
        return 0
    page_no = prov_list[0].get("page_no", 1)
    return max(0, int(page_no) - 1)  # docling은 1-based


# ─────────────────────────────────────────────
# 메인 파서
# ─────────────────────────────────────────────

def parse(pdf_path: str | Path, config: Config = DEFAULT_CONFIG) -> DoclingResult:
    """
    Docling으로 PDF 전체를 분석하여 문서 구조(레이아웃)를 반환한다.

    Parameters
    ----------
    pdf_path : str | Path
        파싱할 PDF 파일 경로
    config : Config
        전역 설정 객체

    Returns
    -------
    DoclingResult
        요소 유형(heading/text/table 등) + 좌표 + 텍스트 포함 결과
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    pdf_path = Path(pdf_path)

    # ── Docling 파이프라인 설정 ──────────────────────
    pipeline_opts = PdfPipelineOptions()
    pipeline_opts.do_ocr = config.parser.docling_ocr_enabled
    pipeline_opts.do_table_structure = True           # 표 구조 분석 활성화
    pipeline_opts.table_structure_options.do_cell_matching = True  # 셀 텍스트 매칭

    # GPU 사용 여부 (CUDA 환경)
    if config.parser.docling_use_gpu:
        pipeline_opts.accelerator_options = None  # GPU 자동 감지

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_opts)
        }
    )

    # ── 변환 실행 ────────────────────────────────────
    conv_result = converter.convert(str(pdf_path))
    doc = conv_result.document  # DoclingDocument 객체

    result = DoclingResult(
        pdf_path=str(pdf_path),
        page_count=len(doc.pages) if hasattr(doc, "pages") else 0,
    )

    # ── 요소 순회 ────────────────────────────────────
    elem_index = 0
    for item, _level in doc.iterate_items():
        # item: DocItem 서브클래스 (TextItem, TableItem, ...)
        label_str = type(item).__name__  # 클래스 이름으로 레이블 추정
        label = _normalize_label(label_str)

        # docling v2: item.label 속성이 있으면 우선 사용
        if hasattr(item, "label"):
            raw_label = str(item.label.value if hasattr(item.label, "value") else item.label)
            label = _normalize_label(raw_label)

        # ── 텍스트 추출 ──
        text = ""
        if hasattr(item, "text"):
            text = str(item.text or "")
        elif hasattr(item, "export_to_markdown"):
            # 표 같은 복합 요소는 마크다운으로 내보내기 시도
            try:
                text = item.export_to_markdown()
            except Exception:
                pass

        # ── 위치(prov) 추출 ──
        prov_list = []
        if hasattr(item, "prov"):
            prov_list = [
                (p.model_dump() if hasattr(p, "model_dump") else vars(p))
                for p in (item.prov if isinstance(item.prov, list) else [item.prov])
                if p is not None
            ]

        page_number = _get_page_number(prov_list)
        bbox = _element_to_bbox(prov_list)

        # ── heading 레벨 추출 ──
        level: int | None = None
        if label in HEADING_LABELS:
            # item 자체에 level 속성이 있는 경우 (docling v2)
            if hasattr(item, "level"):
                raw_level = item.level
                if isinstance(raw_level, int):
                    level = max(1, min(raw_level, 6))
                elif raw_level is not None:
                    try:
                        level = max(1, min(int(raw_level), 6))
                    except (ValueError, TypeError):
                        level = None

        # ── 표 데이터 추출 ──
        table_data: list[list[str]] | None = None
        if label == "table":
            table_data = _parse_table_data(item)

        docling_elem = DoclingElement(
            label=label,
            text=text.strip(),
            page_number=page_number,
            bbox=bbox,
            level=level,
            element_index=elem_index,
            table_data=table_data,
        )

        result.elements.append(docling_elem)

        # 페이지별 인덱스에도 추가
        if page_number not in result.pages:
            result.pages[page_number] = []
        result.pages[page_number].append(docling_elem)

        elem_index += 1

    return result
