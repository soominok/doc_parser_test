"""
parsers/pymupdf_parser.py
─────────────────────────────────────────────────────────────────────────────
PyMuPDF(fitz)를 사용하여 PDF의 텍스트와 폰트 메타데이터를 추출한다.

[역할]
  - 각 텍스트 스팬(span)마다 폰트 이름, 폰트 크기, 굵기(bold), 이탤릭,
    색상, 페이지 내 좌표(bbox)를 함께 추출한다.
  - 이 정보는 heading_detector가 헤딩 여부를 판별하는 핵심 입력이 된다.

[왜 PyMuPDF인가?]
  - pdfplumber는 텍스트 추출 정확도는 높지만 폰트 크기·굵기 정보가 없다.
  - docling은 구조 인식에 특화되어 있어 raw 폰트 정보를 주지 않는다.
  - PyMuPDF는 낮은 수준의 PDF 오브젝트에 직접 접근하여 폰트 메타데이터를
    정확하게 반환한다.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from config import Config, DEFAULT_CONFIG


# ─────────────────────────────────────────────
# 데이터 모델
# ─────────────────────────────────────────────

@dataclass
class SpanInfo:
    """PDF 한 스팬(span)의 텍스트 + 폰트 메타데이터"""

    page_number: int          # 0-based 페이지 번호
    block_index: int          # 페이지 내 블록 순번
    line_index: int           # 블록 내 줄 순번
    span_index: int           # 줄 내 스팬 순번

    text: str                 # 실제 텍스트 (strip 전)
    font_name: str            # 폰트 이름 (예: "NanumGothicBold")
    font_size: float          # 폰트 크기 (pt)
    is_bold: bool             # 굵기 여부 (폰트명 or flags로 판단)
    is_italic: bool           # 이탤릭 여부
    color: int                # 텍스트 색상 (RGB int)

    # 페이지 내 좌표 (left, top, right, bottom) in pt
    bbox: tuple[float, float, float, float] = field(default_factory=tuple)

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def text_stripped(self) -> str:
        return self.text.strip()


@dataclass
class BlockInfo:
    """PDF 한 텍스트 블록의 집계 정보 (여러 스팬을 묶음)"""

    page_number: int
    block_index: int
    bbox: tuple[float, float, float, float]

    # 블록 내 전체 텍스트 (스팬 순서대로 합침)
    text: str

    # 블록 내 모든 스팬 목록
    spans: list[SpanInfo] = field(default_factory=list)

    # 블록의 대표 폰트 크기 (가장 큰 값 또는 최빈값)
    dominant_font_size: float = 0.0
    dominant_font_name: str = ""
    is_bold: bool = False
    is_italic: bool = False

    @property
    def y0(self) -> float:
        return self.bbox[1]


@dataclass
class PyMuPDFResult:
    """페이지별 블록 목록을 담은 최종 결과"""

    pdf_path: str
    page_count: int
    # 페이지 번호(0-based) → 블록 목록
    pages: dict[int, list[BlockInfo]] = field(default_factory=dict)
    # 문서 전체 폰트 크기 히스토그램 {size: 등장 횟수}
    font_size_histogram: dict[float, int] = field(default_factory=dict)


# ─────────────────────────────────────────────
# 파서 구현
# ─────────────────────────────────────────────

def _is_bold_font(font_name: str, flags: int) -> bool:
    """
    폰트 이름 또는 PDF flags로 굵기 여부를 판단한다.

    PDF flags 비트 구성 (fitz 기준):
      bit 0 : superscript
      bit 1 : italic
      bit 4 : bold (16 = 0b10000)
    """
    # flags 기반 판단
    if flags & (1 << 4):   # bold flag
        return True
    # 폰트 이름 기반 판단 (한국 폰트 포함)
    bold_keywords = ["bold", "Bold", "BOLD", "heavy", "Heavy",
                     "black", "Black", "thick", "Thick",
                     "ExtraBold", "SemiBold", "Medium",
                     "굵게", "진하게"]
    return any(kw in font_name for kw in bold_keywords)


def _is_italic_font(font_name: str, flags: int) -> bool:
    """이탤릭 여부 판단"""
    if flags & (1 << 1):
        return True
    return any(kw in font_name for kw in ["italic", "Italic", "oblique", "Oblique"])


def _round_font_size(size: float) -> float:
    """폰트 크기를 0.5pt 단위로 반올림하여 노이즈를 줄인다"""
    return round(size * 2) / 2


def _extract_block_info(
    page_number: int,
    block: dict[str, Any],
    block_index: int,
) -> BlockInfo | None:
    """
    fitz page.get_text("rawdict")의 블록 딕셔너리를 BlockInfo로 변환한다.

    rawdict 구조:
      block → lines → spans → chars
    텍스트 타입(type=0)만 처리하고, 이미지 블록(type=1)은 None 반환.
    """
    if block.get("type") != 0:
        # 이미지 블록은 건너뜀
        return None

    spans_info: list[SpanInfo] = []
    all_text_parts: list[str] = []

    for line_idx, line in enumerate(block.get("lines", [])):
        for span_idx, span in enumerate(line.get("spans", [])):
            raw_text = span.get("text", "")
            font_name = span.get("font", "")
            font_size = _round_font_size(span.get("size", 0.0))
            flags = span.get("flags", 0)
            color = span.get("color", 0)
            bbox = tuple(span.get("bbox", (0, 0, 0, 0)))

            s = SpanInfo(
                page_number=page_number,
                block_index=block_index,
                line_index=line_idx,
                span_index=span_idx,
                text=raw_text,
                font_name=font_name,
                font_size=font_size,
                is_bold=_is_bold_font(font_name, flags),
                is_italic=_is_italic_font(font_name, flags),
                color=color,
                bbox=bbox,
            )
            spans_info.append(s)
            all_text_parts.append(raw_text)

    if not spans_info:
        return None

    # 블록 대표 폰트: 가장 큰 폰트 크기를 가진 스팬의 값 사용
    dominant_span = max(spans_info, key=lambda s: s.font_size)

    return BlockInfo(
        page_number=page_number,
        block_index=block_index,
        bbox=tuple(block.get("bbox", (0, 0, 0, 0))),
        text="".join(all_text_parts),
        spans=spans_info,
        dominant_font_size=dominant_span.font_size,
        dominant_font_name=dominant_span.font_name,
        is_bold=dominant_span.is_bold,
        is_italic=dominant_span.is_italic,
    )


def parse(pdf_path: str | Path, config: Config = DEFAULT_CONFIG) -> PyMuPDFResult:
    """
    PDF 파일 전체를 파싱하여 페이지별 블록·폰트 메타데이터를 반환한다.

    Parameters
    ----------
    pdf_path : str | Path
        파싱할 PDF 파일 경로
    config : Config
        전역 설정 객체

    Returns
    -------
    PyMuPDFResult
        각 페이지의 BlockInfo 목록과 문서 전체 폰트 크기 히스토그램
    """
    pdf_path = Path(pdf_path)
    result = PyMuPDFResult(
        pdf_path=str(pdf_path),
        page_count=0,
    )

    doc = fitz.open(str(pdf_path))
    result.page_count = doc.page_count

    max_pages = config.parser.max_pages
    pages_to_process = range(min(doc.page_count, max_pages) if max_pages else doc.page_count)

    for page_num in pages_to_process:
        page = doc[page_num]

        # rawdict: 폰트 정보까지 포함한 가장 상세한 텍스트 딕셔너리
        raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        blocks: list[BlockInfo] = []

        for block_idx, block in enumerate(raw.get("blocks", [])):
            block_info = _extract_block_info(page_num, block, block_idx)
            if block_info is None:
                continue

            # 빈 블록 제외
            if not block_info.text.strip():
                continue

            blocks.append(block_info)

            # 폰트 크기 히스토그램 집계
            for span in block_info.spans:
                size = span.font_size
                result.font_size_histogram[size] = (
                    result.font_size_histogram.get(size, 0) + 1
                )

        # y좌표 기준 정렬 (위에서 아래 읽기 순서)
        blocks.sort(key=lambda b: (b.y0, b.bbox[0]))
        result.pages[page_num] = blocks

    doc.close()
    return result
