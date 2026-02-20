#!/usr/bin/env python3
"""
PDF → Markdown 하이브리드 파서 (단일 파일 실행)
================================================
법률, 내규, 매뉴얼 등 다양한 형식의 PDF를 정확하게 Markdown으로 변환합니다.

[라이브러리 조합 전략]
  - docling    : 문서 구조 파악 (heading/표/리스트 감지 및 순서 유지)
  - pymupdf    : 폰트 크기/굵기 분석 → heading 계층 구조(H1/H2/H3) 정밀 결정
  - pdfplumber : 정확한 텍스트 추출 / 표 데이터 보완
  - anthropic  : (선택) LLM 기반 최종 정제 (잘린 텍스트, 오류 heading 수정)

[UI]
  - Gradio 웹 UI (브라우저 자동 실행, 별도 서버 설치 불필요)

[실행 방법]
  pip install docling pymupdf pdfplumber gradio anthropic
  python pdf_parser.py

[파일 구조 - 단일 파일 내부 구성]
  1. 상수 & 설정
  2. FontAnalyzer        - PyMuPDF 기반 폰트 분석기
  3. DoclingExtractor    - Docling 기반 구조 추출기
  4. PlumberExtractor    - pdfplumber 기반 텍스트/표 추출기
  5. HybridParser        - 세 결과를 통합하는 핵심 파서
  6. LLMRefiner          - Claude API 기반 후처리기
  7. create_ui()         - Gradio UI 빌더
  8. main()              - 진입점
"""

# ── 표준 라이브러리 ──────────────────────────────────────────────────────────
import os
import re
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional
from collections import Counter
from dataclasses import dataclass, field

# ── 서드파티: PDF 처리 ────────────────────────────────────────────────────────
try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
    print("[경고] PyMuPDF 미설치: pip install pymupdf")

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False
    print("[경고] pdfplumber 미설치: pip install pdfplumber")

try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.document import DocItem
    DOCLING_AVAILABLE = True
except ImportError:
    DOCLING_AVAILABLE = False
    print("[경고] docling 미설치: pip install docling")

# ── 서드파티: UI ──────────────────────────────────────────────────────────────
try:
    import gradio as gr
    GRADIO_AVAILABLE = True
except ImportError:
    GRADIO_AVAILABLE = False
    print("[경고] Gradio 미설치: pip install gradio")

# ── 서드파티: LLM ─────────────────────────────────────────────────────────────
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# ── 로깅 설정 ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)


# ============================================================
# 1. 데이터 클래스
# ============================================================

@dataclass
class DocElement:
    """
    파싱된 문서의 단일 요소를 나타냅니다.
    각 요소는 타입(heading/table/paragraph/list_item)과 내용을 가집니다.
    """
    elem_type: str          # 'heading' | 'table' | 'paragraph' | 'list_item'
    text: str               # 텍스트 내용 (표는 마크다운 문자열)
    page: int               # 0-indexed 페이지 번호
    level: int = 0          # heading 레벨 (1/2/3), 나머지는 0
    font_size: float = 0.0  # PyMuPDF에서 추출한 폰트 크기
    is_bold: bool = False   # 굵기 여부
    confidence: float = 1.0 # 정확도 신뢰도 (LLM 처리 기준)


# ============================================================
# 2. FontAnalyzer - PyMuPDF 기반 폰트 분석기
# ============================================================

class FontAnalyzer:
    """
    PyMuPDF(fitz)를 사용하여 PDF 전체의 폰트 정보를 분석합니다.

    핵심 기능:
    - 문서 전체 폰트 크기 분포 분석 → body 폰트 크기 자동 감지
    - 폰트 크기 클러스터링으로 H1/H2/H3 경계값 자동 결정
    - 페이지별 텍스트+폰트 정보 추출 (텍스트 매칭용)

    [heading 계층 결정 로직]
    body보다 큰 폰트 크기를 내림차순으로 정렬:
      1위 크기 → H1, 2위 크기 → H2, 3위 이하 → H3
    body 크기이지만 bold → H3 (소제목 처리)
    """

    def __init__(self, pdf_path: str):
        if not PYMUPDF_AVAILABLE:
            raise ImportError("PyMuPDF가 필요합니다: pip install pymupdf")

        self.pdf_path = pdf_path
        self.doc = fitz.open(pdf_path)
        self.total_pages = len(self.doc)

        # 폰트 크기 → heading 레벨 매핑 (분석 후 채워짐)
        self.heading_size_map: dict[float, int] = {}
        self.body_size: float = 10.0

        self._analyze_font_distribution()

    def _analyze_font_distribution(self):
        """
        문서 전체를 스캔하여 폰트 크기 분포를 파악합니다.
        가장 빈도 높은 크기 = body, 더 큰 크기들 = heading 후보
        """
        all_sizes = []

        for page_num in range(self.total_pages):
            page = self.doc[page_num]
            # "dict" 모드: 블록→라인→스팬 구조로 텍스트 + 폰트 정보 추출
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:  # type 0 = 텍스트 블록
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        # 의미 있는 텍스트만 포함 (숫자만인 경우 제외 = 페이지 번호 방지)
                        if text and not text.isdigit() and len(text) > 1:
                            all_sizes.append(round(span["size"], 1))

        if not all_sizes:
            logger.warning("폰트 정보를 추출할 수 없습니다.")
            return

        # 가장 많이 쓰인 폰트 크기 = body
        counter = Counter(all_sizes)
        self.body_size = counter.most_common(1)[0][0]
        logger.info(f"body 폰트 크기 감지: {self.body_size}pt")

        # body보다 큰 폰트 크기 목록 (내림차순)
        heading_candidates = sorted(
            {s for s in all_sizes if s > self.body_size * 1.08},
            reverse=True
        )

        # 유사한 크기 클러스터링 (0.5pt 이내 차이는 같은 레벨)
        clustered = self._cluster_sizes(heading_candidates, tolerance=0.8)
        logger.info(f"heading 크기 클러스터: {clustered}")

        # 상위 3개 클러스터에 H1/H2/H3 배정
        for level, cluster in enumerate(clustered[:3], start=1):
            for size in cluster:
                self.heading_size_map[size] = level

    def _cluster_sizes(self, sizes: list[float], tolerance: float) -> list[list[float]]:
        """
        유사한 폰트 크기를 하나의 클러스터로 묶습니다.
        예: [18.0, 17.8, 14.0, 12.5] → [[18.0, 17.8], [14.0], [12.5]]
        """
        if not sizes:
            return []

        clusters = []
        current_cluster = [sizes[0]]

        for size in sizes[1:]:
            if abs(size - current_cluster[-1]) <= tolerance:
                current_cluster.append(size)
            else:
                clusters.append(current_cluster)
                current_cluster = [size]

        clusters.append(current_cluster)
        return clusters

    def get_page_text_with_fonts(self, page_num: int) -> list[dict]:
        """
        특정 페이지의 텍스트를 라인 단위로 추출합니다 (폰트 정보 포함).

        Returns:
            list[dict]: [
                {
                    'text': str,       # 라인 텍스트
                    'size': float,     # 폰트 크기 (최댓값)
                    'bold': bool,      # 굵기 여부
                    'bbox': tuple,     # (x0, y0, x1, y1)
                    'page': int        # 0-indexed 페이지
                }, ...
            ]
        """
        if page_num >= self.total_pages:
            return []

        page = self.doc[page_num]
        result = []

        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                line_text = ""
                max_size = 0.0
                is_bold = False

                for span in line["spans"]:
                    line_text += span["text"]
                    max_size = max(max_size, span["size"])

                    # flags 비트 16 = bold, 또는 폰트 이름에 "Bold" 포함
                    flags = span.get("flags", 0)
                    font_name = span.get("font", "")
                    if (flags & 16) or "bold" in font_name.lower():
                        is_bold = True

                line_text = line_text.strip()
                if line_text and max_size > 0:
                    result.append({
                        "text": line_text,
                        "size": round(max_size, 1),
                        "bold": is_bold,
                        "bbox": line["bbox"],
                        "page": page_num,
                    })

        return result

    def resolve_heading_level(self, font_size: float, is_bold: bool) -> Optional[int]:
        """
        주어진 폰트 크기와 굵기 정보로 heading 레벨을 반환합니다.

        Returns:
            int (1/2/3) 또는 None (heading이 아님)
        """
        # 1. 크기 매핑에서 정확히 찾기 (허용 오차 0.5pt)
        for size, level in self.heading_size_map.items():
            if abs(font_size - size) <= 0.5:
                return level

        # 2. body보다 크면 H3로 폴백
        if font_size > self.body_size * 1.08:
            return 3

        # 3. body 크기이지만 bold → 소제목으로 H3 처리
        if is_bold and font_size >= self.body_size:
            return 3

        return None  # heading 아님

    def close(self):
        """PyMuPDF 문서 리소스를 해제합니다."""
        if self.doc:
            self.doc.close()


# ============================================================
# 3. DoclingExtractor - Docling 기반 구조 추출기
# ============================================================

class DoclingExtractor:
    """
    Docling을 사용하여 PDF 문서의 논리적 구조를 추출합니다.

    Docling 장점:
    - 컬럼 레이아웃, 다단 구성 등 복잡한 구조 인식
    - heading/표/리스트/본문 분류 정확도 높음
    - 요소 순서(읽기 순서) 보존

    Docling 단점 (이 클래스에서 해결하지 않음 → HybridParser에서 보완):
    - heading 계층 구조 미구분 (모든 heading이 동일 레벨)
    - 텍스트 잘림/순서 뒤바뀜 일부 발생
    - 한글 폰트 환경에서 인코딩 오류 가능성

    추출 결과는 DocElement 리스트로 반환되며,
    문서 내 원래 순서가 유지됩니다.
    """

    # Docling 요소 타입 이름 → 내부 타입 문자열 매핑
    _TYPE_MAP = {
        "SectionHeaderItem": "heading",
        "HeadingItem": "heading",
        "TextItem": "paragraph",
        "ParagraphItem": "paragraph",
        "TableItem": "table",
        "ListItem": "list_item",
        "PictureItem": None,      # 이미지는 무시
        "PageHeaderItem": None,   # 페이지 헤더 무시
        "PageFooterItem": None,   # 페이지 푸터 무시
    }

    def __init__(self, pdf_path: str):
        if not DOCLING_AVAILABLE:
            raise ImportError("docling이 필요합니다: pip install docling")
        self.pdf_path = pdf_path
        self._converter = self._build_converter()

    def _build_converter(self) -> "DocumentConverter":
        """Docling 변환기를 초기화합니다."""
        options = PdfPipelineOptions()
        options.do_ocr = False           # 일반 텍스트 PDF → OCR 불필요
        options.do_table_structure = True  # 표 구조 분석 활성화

        return DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=options)
            }
        )

    def extract(self) -> list[DocElement]:
        """
        Docling으로 PDF를 분석하고 DocElement 리스트를 반환합니다.
        요소는 문서 내 읽기 순서대로 정렬됩니다.

        Returns:
            list[DocElement]: 순서가 보존된 문서 요소 목록
        """
        logger.info("Docling 구조 분석 시작...")
        result = self._converter.convert(self.pdf_path)
        doc = result.document

        elements: list[DocElement] = []

        # iterate_items()는 (item, level) 튜플을 생성합니다.
        # level은 중첩 depth (heading 레벨이 아님)
        for item, _level in doc.iterate_items():
            elem_type_name = type(item).__name__
            mapped_type = self._TYPE_MAP.get(elem_type_name)

            # 무시할 타입 스킵
            if mapped_type is None:
                continue

            # 페이지 번호 추출 (0-indexed로 변환)
            page_no = 0
            if hasattr(item, "prov") and item.prov:
                try:
                    page_no = max(0, item.prov[0].page_no - 1)
                except (AttributeError, IndexError):
                    page_no = 0

            # ── 표 처리 ──────────────────────────────────────────
            if mapped_type == "table":
                try:
                    table_md = item.export_to_markdown()
                    if table_md.strip():
                        elements.append(DocElement(
                            elem_type="table",
                            text=table_md,
                            page=page_no,
                        ))
                except Exception as e:
                    logger.warning(f"표 마크다운 변환 실패 (page {page_no}): {e}")
                continue

            # ── 텍스트 요소 처리 ──────────────────────────────────
            text = ""
            if hasattr(item, "text") and item.text:
                text = item.text.strip()
            elif hasattr(item, "export_to_markdown"):
                text = item.export_to_markdown().strip()

            if not text:
                continue

            # heading 레벨 초기값 (docling은 구분 없으므로 1로 설정,
            # 이후 FontAnalyzer에서 실제 레벨로 교체됨)
            level = 1 if mapped_type == "heading" else 0

            elements.append(DocElement(
                elem_type=mapped_type,
                text=text,
                page=page_no,
                level=level,
            ))

        logger.info(
            f"Docling 완료: 총 {len(elements)}개 요소 "
            f"(heading: {sum(1 for e in elements if e.elem_type == 'heading')}, "
            f"table: {sum(1 for e in elements if e.elem_type == 'table')})"
        )
        return elements


# ============================================================
# 4. PlumberExtractor - pdfplumber 기반 정확한 텍스트 추출기
# ============================================================

class PlumberExtractor:
    """
    pdfplumber를 사용하여 정확한 텍스트와 표를 추출합니다.

    pdfplumber 장점:
    - 문자 단위 추출로 텍스트 잘림 없음
    - 표 경계선 감지 및 셀 병합 처리
    - 텍스트 좌표 정보로 레이아웃 분석 가능

    pdfplumber 단점 (이 클래스에서 해결하지 않음):
    - heading 인식 불가 (단순 텍스트로 처리)
    - 문서 구조(섹션/계층) 파악 불가

    이 클래스는 주로 두 가지 용도로 사용됩니다:
    1. 특정 페이지의 전체 텍스트를 정확하게 추출 (Docling 텍스트 검증용)
    2. 표 데이터 추출 (Docling 표 오류 보완용)
    """

    def __init__(self, pdf_path: str):
        if not PDFPLUMBER_AVAILABLE:
            raise ImportError("pdfplumber가 필요합니다: pip install pdfplumber")
        self.pdf_path = pdf_path

    def get_total_pages(self) -> int:
        """총 페이지 수를 반환합니다."""
        with pdfplumber.open(self.pdf_path) as pdf:
            return len(pdf.pages)

    def extract_page_text(self, page_num: int) -> str:
        """
        특정 페이지의 전체 텍스트를 추출합니다.
        텍스트 순서(위→아래, 좌→우)가 정확하게 유지됩니다.

        Args:
            page_num: 0-indexed 페이지 번호

        Returns:
            str: 해당 페이지의 전체 텍스트
        """
        with pdfplumber.open(self.pdf_path) as pdf:
            if page_num >= len(pdf.pages):
                return ""
            page = pdf.pages[page_num]
            text = page.extract_text(x_tolerance=3, y_tolerance=3)
            return text or ""

    def extract_page_tables(self, page_num: int) -> list[str]:
        """
        특정 페이지의 모든 표를 마크다운 형식으로 추출합니다.

        Args:
            page_num: 0-indexed 페이지 번호

        Returns:
            list[str]: 마크다운 형식의 표 목록
        """
        tables_md = []

        with pdfplumber.open(self.pdf_path) as pdf:
            if page_num >= len(pdf.pages):
                return []
            page = pdf.pages[page_num]

            for table in page.extract_tables():
                if not table:
                    continue
                md = self._table_to_markdown(table)
                if md:
                    tables_md.append(md)

        return tables_md

    def _table_to_markdown(self, table_data: list[list]) -> str:
        """
        2D 리스트 형식의 표 데이터를 마크다운 표로 변환합니다.

        Args:
            table_data: [[cell, cell, ...], [cell, cell, ...], ...]

        Returns:
            str: 마크다운 표 문자열
        """
        if not table_data:
            return ""

        lines = []
        for i, row in enumerate(table_data):
            # None 셀을 빈 문자열로 처리, 개행 문자를 공백으로 치환
            clean_cells = [
                str(cell).strip().replace("\n", " ") if cell is not None else ""
                for cell in row
            ]
            lines.append("| " + " | ".join(clean_cells) + " |")

            # 헤더 행(첫 번째 행) 다음에 구분선 삽입
            if i == 0:
                separator = "| " + " | ".join(["---"] * len(clean_cells)) + " |"
                lines.append(separator)

        return "\n".join(lines)

    def extract_chars_with_font(self, page_num: int) -> list[dict]:
        """
        페이지의 문자 단위 폰트 정보를 추출합니다 (고정밀 폰트 분석용).
        PyMuPDF 폰트 분석을 보완하는 용도로 사용합니다.

        Returns:
            list[dict]: [{'text', 'fontname', 'size', 'x0', 'top'}, ...]
        """
        with pdfplumber.open(self.pdf_path) as pdf:
            if page_num >= len(pdf.pages):
                return []
            page = pdf.pages[page_num]
            return page.chars or []


# ============================================================
# 5. HybridParser - 핵심 통합 파서
# ============================================================

class HybridParser:
    """
    Docling + PyMuPDF + pdfplumber를 통합하는 핵심 하이브리드 파서.

    [통합 전략 - 4단계]
    Step 1. Docling으로 문서 구조 추출 (요소 순서, 타입 분류)
    Step 2. PyMuPDF 폰트 분석으로 heading 레벨 정밀 결정
    Step 3. pdfplumber 텍스트로 Docling 텍스트 정확도 보완
    Step 4. (선택) LLM으로 최종 정제

    [heading 계층 결정 방법]
    - Docling이 "heading"으로 분류한 요소를 찾음
    - 해당 요소의 텍스트를 PyMuPDF 페이지 데이터에서 검색 (퍼지 매칭)
    - 매칭된 텍스트의 폰트 크기로 FontAnalyzer.resolve_heading_level() 호출
    - 매칭 실패 시: Docling 원본 레벨 유지

    [텍스트 정확도 보완 방법]
    - 단락 텍스트가 너무 짧거나 이상한 경우 (신뢰도 낮음)
    - pdfplumber 해당 페이지 전체 텍스트에서 유사 문장 검색하여 교체
    """

    # heading 매칭 시 사용할 유사도 임계값 (0.0 ~ 1.0)
    HEADING_MATCH_THRESHOLD = 0.45

    def __init__(
        self,
        pdf_path: str,
        use_llm: bool = False,
        api_key: str = "",
        llm_model: str = "claude-opus-4-6",
    ):
        self.pdf_path = pdf_path
        self.use_llm = use_llm
        self.api_key = api_key
        self.llm_model = llm_model

        # 각 추출기 초기화
        self.font_analyzer = FontAnalyzer(pdf_path) if PYMUPDF_AVAILABLE else None
        self.plumber = PlumberExtractor(pdf_path) if PDFPLUMBER_AVAILABLE else None
        self.total_pages = (
            self.plumber.get_total_pages() if self.plumber else 0
        )

    def parse(self, progress_cb=None) -> str:
        """
        PDF 전체를 파싱하여 마크다운 문자열을 반환합니다.

        Args:
            progress_cb: 진행률 콜백 함수 (value: float, desc: str) → None

        Returns:
            str: 최종 마크다운 텍스트
        """

        def _progress(value: float, desc: str):
            if progress_cb:
                progress_cb(value, desc)
            logger.info(f"[{int(value * 100)}%] {desc}")

        _progress(0.05, "Docling 구조 분석 중...")

        # ── Step 1: Docling 구조 추출 ────────────────────────────
        if not DOCLING_AVAILABLE:
            raise RuntimeError("docling이 설치되지 않았습니다.")

        docling_extractor = DoclingExtractor(self.pdf_path)
        elements: list[DocElement] = docling_extractor.extract()

        _progress(0.40, "PyMuPDF 폰트 분석 중 (heading 계층 결정)...")

        # ── Step 2: PyMuPDF로 heading 레벨 결정 ─────────────────
        if self.font_analyzer:
            # 페이지별 폰트 정보를 미리 로드
            page_font_cache: dict[int, list[dict]] = {}
            for pg in range(self.total_pages):
                page_font_cache[pg] = self.font_analyzer.get_page_text_with_fonts(pg)

            elements = self._resolve_heading_levels(elements, page_font_cache)
        else:
            logger.warning("PyMuPDF 없음 → heading 레벨을 Docling 기본값 사용")

        _progress(0.65, "pdfplumber 텍스트 정확도 보완 중...")

        # ── Step 3: pdfplumber로 텍스트 보완 ────────────────────
        if self.plumber:
            elements = self._enhance_text_with_plumber(elements)
        else:
            logger.warning("pdfplumber 없음 → 텍스트 보완 생략")

        _progress(0.80, "마크다운 빌드 중...")

        # ── Step 4: 마크다운 조합 ────────────────────────────────
        markdown = self._build_markdown(elements)

        # ── Step 5 (선택): LLM 정제 ──────────────────────────────
        if self.use_llm and self.api_key and ANTHROPIC_AVAILABLE:
            _progress(0.88, "LLM으로 최종 정제 중 (시간이 걸릴 수 있습니다)...")
            refiner = LLMRefiner(api_key=self.api_key, model=self.llm_model)
            markdown = refiner.refine(markdown)

        _progress(1.0, "변환 완료!")

        # 리소스 정리
        if self.font_analyzer:
            self.font_analyzer.close()

        return markdown

    # ── 내부 메서드 ──────────────────────────────────────────────────

    def _resolve_heading_levels(
        self,
        elements: list[DocElement],
        page_font_cache: dict[int, list[dict]],
    ) -> list[DocElement]:
        """
        Docling이 감지한 heading 요소의 실제 레벨을 PyMuPDF 폰트 정보로 결정합니다.

        동작 원리:
        1. heading 타입 요소를 찾음
        2. 해당 페이지의 PyMuPDF 라인 데이터에서 유사 텍스트 검색
        3. 매칭된 라인의 폰트 크기로 레벨 결정
        4. 매칭 실패 시 레벨 1 유지
        """
        resolved = []

        for elem in elements:
            if elem.elem_type != "heading":
                resolved.append(elem)
                continue

            page_lines = page_font_cache.get(elem.page, [])
            best_match = self._fuzzy_find(elem.text, page_lines)

            if best_match:
                level = self.font_analyzer.resolve_heading_level(
                    best_match["size"], best_match["bold"]
                )
                if level:
                    elem.level = level
                    elem.font_size = best_match["size"]
                    elem.is_bold = best_match["bold"]
                # level이 None이면 기존 레벨(1) 유지
            # else: 매칭 실패 → Docling이 heading으로 판단했으므로 레벨 1 유지

            resolved.append(elem)

        return resolved

    def _fuzzy_find(
        self,
        target: str,
        lines: list[dict],
    ) -> Optional[dict]:
        """
        target 텍스트와 가장 유사한 폰트 라인을 lines에서 찾습니다.

        Docling 텍스트가 잘리거나 약간 변형될 수 있으므로
        완전 일치가 아닌 포함 관계 기반 유사도를 사용합니다.

        Args:
            target: Docling에서 추출한 heading 텍스트
            lines: PyMuPDF에서 추출한 페이지 라인 목록

        Returns:
            가장 유사한 라인 dict 또는 None
        """
        if not target or not lines:
            return None

        # 공백 정규화
        t_clean = re.sub(r"\s+", " ", target.strip()).lower()
        best_ratio = 0.0
        best_line = None

        for line in lines:
            l_clean = re.sub(r"\s+", " ", line["text"].strip()).lower()
            if not l_clean:
                continue

            # 포함 관계 비율 계산
            if t_clean in l_clean:
                ratio = len(t_clean) / len(l_clean)
            elif l_clean in t_clean:
                ratio = len(l_clean) / len(t_clean)
            else:
                # 공통 단어 비율 (간단한 자카드 유사도)
                t_words = set(t_clean.split())
                l_words = set(l_clean.split())
                if not t_words or not l_words:
                    continue
                intersection = t_words & l_words
                union = t_words | l_words
                ratio = len(intersection) / len(union)

            if ratio > best_ratio:
                best_ratio = ratio
                best_line = line

        return best_line if best_ratio >= self.HEADING_MATCH_THRESHOLD else None

    def _enhance_text_with_plumber(
        self, elements: list[DocElement]
    ) -> list[DocElement]:
        """
        pdfplumber의 텍스트로 Docling 단락 텍스트의 정확도를 보완합니다.

        보완 기준:
        - 표(table) 요소는 건드리지 않음 (이미 마크다운 형식)
        - heading은 텍스트 자체가 중요하므로 보완하지 않음
        - paragraph의 텍스트가 비정상적으로 짧거나 (3글자 미만) 잘린 경우,
          같은 페이지 pdfplumber 텍스트에서 더 나은 버전 탐색
        - 기본 동작: 텍스트 정리(클리닝)만 수행

        참고: 전체 텍스트 교체보다는 Docling 텍스트를 우선하고
        명백히 잘린 경우만 보완하는 보수적 전략을 사용합니다.
        """
        # 페이지별 pdfplumber 텍스트 캐시
        plumber_text_cache: dict[int, str] = {}

        enhanced = []

        for elem in elements:
            # 표와 heading은 그대로 유지
            if elem.elem_type in ("table", "heading"):
                enhanced.append(elem)
                continue

            text = elem.text.strip()

            # 텍스트 기본 정리
            text = self._clean_text(text)

            # 텍스트가 매우 짧으면 pdfplumber에서 보완 시도
            if len(text) < 3 and PDFPLUMBER_AVAILABLE:
                pg = elem.page
                if pg not in plumber_text_cache:
                    plumber_text_cache[pg] = self.plumber.extract_page_text(pg)

                # pdfplumber 전체 텍스트에서 해당 단락과 유사한 줄 찾기
                better_text = self._find_better_text(text, plumber_text_cache[pg])
                if better_text:
                    text = better_text

            if text:
                elem.text = text
                enhanced.append(elem)

        return enhanced

    def _clean_text(self, text: str) -> str:
        """
        추출된 텍스트를 정리합니다.
        - 연속 공백 제거
        - 단어 중간 하이픈 연결 (PDF 줄바꿈 처리)
        - 앞뒤 공백 제거
        """
        # 단어 중간 하이픈+개행 제거 (예: "인사-\n제도" → "인사제도")
        text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
        # 개행을 공백으로
        text = text.replace("\n", " ")
        # 연속 공백 → 단일 공백
        text = re.sub(r" {2,}", " ", text)
        return text.strip()

    def _find_better_text(self, short_text: str, full_page_text: str) -> Optional[str]:
        """
        pdfplumber 전체 텍스트에서 short_text와 유사한 더 긴 문장을 찾습니다.
        주로 Docling에서 텍스트가 잘린 경우에 사용합니다.
        """
        if not short_text or not full_page_text:
            return None

        for line in full_page_text.split("\n"):
            line = line.strip()
            if len(line) > len(short_text) and short_text.lower() in line.lower():
                return line

        return None

    def _build_markdown(self, elements: list[DocElement]) -> str:
        """
        DocElement 리스트를 최종 마크다운 문자열로 변환합니다.

        마크다운 형식 규칙:
        - heading  → # / ## / ### (레벨에 따라)
        - table    → 마크다운 표 그대로 출력
        - list_item → - 항목
        - paragraph → 일반 텍스트 (빈 줄로 구분)

        가독성을 위해:
        - heading 앞뒤에 빈 줄 추가
        - 표 앞뒤에 빈 줄 추가
        - 연속 빈 줄은 최대 2개로 제한
        """
        parts: list[str] = []
        prev_type: Optional[str] = None

        for elem in elements:
            t = elem.elem_type

            # ── heading ────────────────────────────────────────
            if t == "heading":
                text = elem.text.strip()
                if not text:
                    continue

                level = max(1, min(3, elem.level))  # 1~3 범위 보정

                # 이전 요소가 paragraph/list라면 빈 줄 먼저 삽입
                if prev_type and prev_type not in ("heading",):
                    parts.append("")

                parts.append(f"{'#' * level} {text}")
                parts.append("")  # heading 뒤 빈 줄

            # ── 표 ──────────────────────────────────────────────
            elif t == "table":
                md = elem.text.strip()
                if not md:
                    continue

                if prev_type:
                    parts.append("")  # 표 앞 빈 줄

                parts.append(md)
                parts.append("")  # 표 뒤 빈 줄

            # ── 리스트 항목 ─────────────────────────────────────
            elif t == "list_item":
                text = elem.text.strip()
                if not text:
                    continue

                # 이미 - 또는 * 로 시작하면 그대로, 아니면 - 추가
                if re.match(r"^[-*•]\s", text):
                    parts.append(text)
                else:
                    parts.append(f"- {text}")

            # ── 일반 단락 ───────────────────────────────────────
            elif t == "paragraph":
                text = elem.text.strip()
                if not text:
                    continue

                # 리스트 다음 단락이면 빈 줄 추가
                if prev_type == "list_item":
                    parts.append("")

                parts.append(text)
                parts.append("")  # 단락 뒤 빈 줄

            if t:
                prev_type = t

        # 연속 빈 줄 2개 이상 → 2개로 통일
        result = "\n".join(parts)
        result = re.sub(r"\n{3,}", "\n\n", result)

        return result.strip()


# ============================================================
# 6. LLMRefiner - Claude API 기반 후처리기
# ============================================================

class LLMRefiner:
    """
    Claude API를 사용하여 마크다운을 최종 정제합니다.

    활용 시나리오:
    - heading 레벨이 여전히 잘못된 경우
    - 표 형식 오류
    - 텍스트 잘림/이어붙이기가 필요한 경우
    - 불필요한 반복 내용 제거

    청크 처리:
    - 긴 문서는 heading을 기준으로 청크로 분할
    - 각 청크를 독립적으로 LLM 처리 후 재조합
    """

    # 한 번에 LLM에 보낼 최대 텍스트 크기 (글자 수)
    MAX_CHUNK_SIZE = 3000

    # LLM 호출 간 대기 시간 (초) - API 레이트 리밋 방지
    CALL_DELAY = 0.5

    SYSTEM_PROMPT = """당신은 PDF에서 추출된 마크다운을 정제하는 전문가입니다.
주어진 마크다운을 아래 기준으로 수정하세요:

1. Heading 레벨 수정: 문서 구조에 맞게 #(H1), ##(H2), ###(H3) 조정
2. 표 형식 수정: 마크다운 표가 올바른 형식인지 확인 및 수정
3. 텍스트 이어붙이기: 잘린 문장을 자연스럽게 연결
4. 중복 제거: 동일한 내용이 반복되면 한 번만 남김
5. 불필요한 요소 제거: 페이지 번호, 머리말/꼬리말 잔재 제거

중요: 수정된 마크다운만 출력하세요. 설명, 주석, 메타 정보는 일절 포함하지 마세요."""

    def __init__(self, api_key: str, model: str = "claude-opus-4-6"):
        if not ANTHROPIC_AVAILABLE:
            raise ImportError("anthropic이 필요합니다: pip install anthropic")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def refine(self, markdown: str) -> str:
        """
        마크다운 전체를 청크로 나눠 LLM으로 정제합니다.

        Args:
            markdown: 정제할 마크다운 문자열

        Returns:
            str: 정제된 마크다운
        """
        if len(markdown) <= self.MAX_CHUNK_SIZE:
            return self._call_api(markdown)

        chunks = self._split_into_chunks(markdown)
        logger.info(f"LLM 정제: {len(chunks)}개 청크 처리")

        refined_chunks = []
        for i, chunk in enumerate(chunks):
            logger.info(f"  청크 {i + 1}/{len(chunks)} 처리 중...")
            refined = self._call_api(chunk)
            refined_chunks.append(refined)

            # 레이트 리밋 방지용 대기
            if i < len(chunks) - 1:
                import time
                time.sleep(self.CALL_DELAY)

        return "\n\n".join(refined_chunks)

    def _call_api(self, text: str) -> str:
        """
        Claude API를 호출하여 텍스트를 정제합니다.

        오류 발생 시 원본 텍스트를 그대로 반환합니다.
        """
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=self.SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": f"다음 마크다운을 정제해주세요:\n\n---\n{text}\n---",
                    }
                ],
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.error(f"LLM API 오류: {e}")
            return text  # 오류 시 원본 반환

    def _split_into_chunks(self, markdown: str) -> list[str]:
        """
        마크다운을 heading 경계에서 청크로 분할합니다.
        각 청크는 MAX_CHUNK_SIZE를 최대한 넘지 않도록 합니다.

        분할 전략:
        - H1(#) 또는 H2(##) heading을 만나면 청크 경계 후보
        - 현재 청크 크기가 절반 이상이면 해당 경계에서 분할
        """
        lines = markdown.split("\n")
        chunks: list[str] = []
        current_lines: list[str] = []
        current_size = 0

        for line in lines:
            is_major_heading = re.match(r"^#{1,2}\s", line)

            # 주요 heading 앞에서 분할 (현재 청크가 충분히 크면)
            if is_major_heading and current_size >= self.MAX_CHUNK_SIZE // 2:
                if current_lines:
                    chunks.append("\n".join(current_lines))
                current_lines = [line]
                current_size = len(line)
            else:
                current_lines.append(line)
                current_size += len(line)

        if current_lines:
            chunks.append("\n".join(current_lines))

        return [c for c in chunks if c.strip()]


# ============================================================
# 7. Gradio UI
# ============================================================

def create_ui():
    """
    Gradio 기반 웹 UI를 생성합니다.

    UI 구성:
    - 왼쪽 패널: PDF 업로드 + 설정 옵션
    - 오른쪽 패널: 마크다운 결과 출력 + 미리보기
    - 하단: 다운로드 버튼

    Gradio를 선택한 이유:
    - 단일 파일로 웹 UI 구현 가능
    - pip install만으로 별도 설정 없이 실행
    - 파일 업로드/다운로드 내장 지원
    - 마크다운 실시간 렌더링 지원
    """
    if not GRADIO_AVAILABLE:
        raise ImportError("Gradio가 필요합니다: pip install gradio")

    # ── 이벤트 핸들러 함수들 ──────────────────────────────────────

    def on_convert(pdf_file, use_llm: bool, api_key: str, progress=gr.Progress()):
        """
        변환 버튼 클릭 시 실행되는 핸들러.

        Args:
            pdf_file: Gradio 업로드 파일 객체
            use_llm: LLM 정제 여부
            api_key: Anthropic API 키
            progress: Gradio Progress 객체 (자동 주입)

        Returns:
            tuple: (마크다운 텍스트, 상태 메시지, 다운로드 파일 경로)
        """
        if pdf_file is None:
            return "", "PDF 파일을 먼저 업로드해주세요.", None

        pdf_path = pdf_file  # Gradio type="filepath"는 경로 문자열 반환

        # 라이브러리 설치 확인
        missing = []
        if not DOCLING_AVAILABLE:
            missing.append("docling")
        if not PYMUPDF_AVAILABLE:
            missing.append("pymupdf")
        if not PDFPLUMBER_AVAILABLE:
            missing.append("pdfplumber")

        if missing:
            return (
                "",
                f"필수 라이브러리가 없습니다: {', '.join(missing)}\n"
                f"pip install {' '.join(missing)} 로 설치해주세요.",
                None,
            )

        if use_llm and not api_key.strip():
            return "", "LLM 사용 시 Anthropic API Key를 입력해주세요.", None

        try:
            parser = HybridParser(
                pdf_path=pdf_path,
                use_llm=use_llm,
                api_key=api_key.strip(),
            )

            def progress_cb(value: float, desc: str):
                progress(value, desc=desc)

            markdown = parser.parse(progress_cb=progress_cb)

            # 출력 디렉토리 결정:
            #   - 환경변수 OUTPUT_DIR 설정 시 해당 경로 사용 (Docker volume mount 용)
            #   - 미설정 시 시스템 임시 디렉토리 사용
            stem = Path(pdf_path).stem
            out_dir = os.environ.get("OUTPUT_DIR", tempfile.gettempdir())
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"{stem}_parsed.md")

            with open(out_path, "w", encoding="utf-8") as f:
                f.write(markdown)

            status = (
                f"변환 완료! "
                f"총 {len(markdown)}자 / "
                f"heading {markdown.count(chr(10) + '#')}개"
            )
            return markdown, status, out_path

        except Exception as e:
            logger.error("파싱 오류", exc_info=True)
            return "", f"오류 발생: {type(e).__name__}: {e}", None

    def on_preview(markdown: str):
        """마크다운 텍스트를 렌더링용으로 반환합니다."""
        return markdown

    # ── UI 레이아웃 ───────────────────────────────────────────────

    with gr.Blocks(
        title="PDF → Markdown 하이브리드 파서",
        theme=gr.themes.Soft(),
        css="""
            .header { text-align: center; padding: 16px 0; }
            .tip { font-size: 0.85em; color: #555; margin-top: 4px; }
        """,
    ) as demo:

        # 상단 제목
        gr.Markdown(
            """
            # PDF → Markdown 하이브리드 파서
            **Docling** (구조) + **PyMuPDF** (폰트 분석) + **pdfplumber** (텍스트 정확도) 조합으로
            법률, 내규, 매뉴얼 등 다양한 PDF를 정확한 마크다운으로 변환합니다.
            """,
            elem_classes=["header"],
        )

        with gr.Row(equal_height=False):
            # ── 왼쪽: 입력 패널 ──────────────────────────────────
            with gr.Column(scale=1, min_width=280):
                gr.Markdown("### 파일 업로드")

                pdf_input = gr.File(
                    label="PDF 파일",
                    file_types=[".pdf"],
                    type="filepath",
                )

                gr.Markdown("### 변환 옵션")

                with gr.Accordion("LLM 정제 (선택 사항)", open=False):
                    use_llm = gr.Checkbox(
                        label="Claude API로 최종 정제 활성화",
                        value=False,
                        info="heading 오류, 텍스트 잘림 등을 AI가 자동 수정합니다.",
                    )
                    api_key = gr.Textbox(
                        label="Anthropic API Key",
                        type="password",
                        placeholder="sk-ant-...",
                        info="LLM 정제 활성화 시 필수 입력",
                    )
                    gr.Markdown(
                        "API Key는 서버에 저장되지 않으며 이 세션에서만 사용됩니다.",
                        elem_classes=["tip"],
                    )

                convert_btn = gr.Button(
                    "변환 시작",
                    variant="primary",
                    size="lg",
                )

                status_text = gr.Textbox(
                    label="상태",
                    interactive=False,
                    lines=2,
                )

                download_file = gr.File(
                    label="결과 파일 다운로드",
                    interactive=False,
                )

            # ── 오른쪽: 결과 패널 ────────────────────────────────
            with gr.Column(scale=2):
                gr.Markdown("### 변환 결과")

                with gr.Tabs():
                    with gr.Tab("마크다운 소스"):
                        md_output = gr.Textbox(
                            label="마크다운 텍스트",
                            lines=35,
                            max_lines=60,
                            placeholder="PDF를 업로드하고 '변환 시작'을 클릭하세요.",
                        )

                    with gr.Tab("렌더링 미리보기"):
                        preview_btn = gr.Button("미리보기 새로고침", size="sm")
                        preview_md = gr.Markdown(
                            value="변환 후 이 탭에서 렌더링 결과를 확인하세요.",
                        )

        # ── 이벤트 연결 ─────────────────────────────────────────────

        convert_btn.click(
            fn=on_convert,
            inputs=[pdf_input, use_llm, api_key],
            outputs=[md_output, status_text, download_file],
        )

        preview_btn.click(
            fn=on_preview,
            inputs=[md_output],
            outputs=[preview_md],
        )

    return demo


# ============================================================
# 8. 메인 진입점
# ============================================================

def main():
    """
    애플리케이션 진입점.
    환경변수로 Docker / 로컬 실행 환경을 자동 감지하여 Gradio를 시작합니다.

    [지원 환경변수]
      DOCKER_ENV   : "true" 이면 Docker 모드 (브라우저 자동 실행 비활성화)
      GRADIO_HOST  : 바인딩 호스트 (기본: 0.0.0.0)
      GRADIO_PORT  : 바인딩 포트   (기본: 7860)
    """
    print("=" * 60)
    print("  PDF → Markdown 하이브리드 파서")
    print("=" * 60)

    # ── Docker 환경 감지 ──────────────────────────────────────────
    # Dockerfile에서 ENV DOCKER_ENV=true 로 설정됨
    is_docker = os.environ.get("DOCKER_ENV", "").lower() in ("1", "true", "yes")

    # ── 서버 설정: 환경변수 우선, 없으면 기본값 ───────────────────
    host = os.environ.get("GRADIO_HOST", "0.0.0.0")
    port = int(os.environ.get("GRADIO_PORT", "7860"))

    # ── 라이브러리 설치 상태 출력 ──────────────────────────────────
    libs = {
        "docling": DOCLING_AVAILABLE,
        "pymupdf": PYMUPDF_AVAILABLE,
        "pdfplumber": PDFPLUMBER_AVAILABLE,
        "gradio": GRADIO_AVAILABLE,
        "anthropic": ANTHROPIC_AVAILABLE,
    }
    for lib, available in libs.items():
        status = "✓" if available else "✗ (미설치)"
        print(f"  {lib:15s}: {status}")

    mode_label = "Docker" if is_docker else "로컬"
    print(f"\n  실행 모드     : {mode_label}")
    print(f"  서버 주소     : http://{host}:{port}")
    if is_docker:
        print(f"  접속 URL      : http://localhost:{port}  (포트 포워딩 후)")
    print()

    if not GRADIO_AVAILABLE:
        print("[오류] Gradio가 필요합니다: pip install gradio")
        return

    demo = create_ui()
    demo.launch(
        server_name=host,             # 모든 네트워크 인터페이스 (컨테이너 외부 접근 허용)
        server_port=port,             # 환경변수 또는 기본 7860
        share=False,                  # 공개 URL 생성 비활성화
        inbrowser=not is_docker,      # Docker 환경에서는 브라우저 자동 실행 비활성화
    )


if __name__ == "__main__":
    main()
