"""
Docling 기반 PDF → Markdown 변환 모듈

Docling(IBM Research)의 AI 기반 문서 구조 분석을 활용하여
PDF를 구조화된 Markdown으로 변환합니다.

역할:
- PDF → Docling Document 객체 변환
- 문서 구조(heading, paragraph, table, list) 인식
- 표를 Markdown 테이블로 변환
- 이미지 위치 감지 및 추출

한계 (후처리 모듈에서 보완):
- heading이 ## 단일 레벨로 출력되는 문제 → heading_processor에서 보완
- 이미지 내 텍스트 미추출 → image_extractor에서 보완
"""
import sys
from pathlib import Path
from typing import Optional

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.utils.logger import get_logger

logger = get_logger(__name__)


class DoclingParser:
    """
    Docling을 사용한 PDF 파서.

    Docling의 DocumentConverter를 래핑하여
    PDF를 Markdown 문자열로 변환합니다.
    """

    def __init__(self, ocr_enabled: bool = True, table_structure: bool = True):
        """
        Args:
            ocr_enabled: OCR 활성화 여부 (이미지 내 텍스트 인식)
            table_structure: 표 구조 분석 활성화 여부
        """
        self.ocr_enabled = ocr_enabled
        self.table_structure = table_structure
        self._converter = None  # lazy initialization

    def _init_converter(self):
        """
        Docling DocumentConverter를 초기화합니다.
        첫 변환 시점에 한 번만 초기화 (모델 로딩이 무거우므로 lazy init).
        """
        if self._converter is not None:
            return

        try:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.datamodel.base_models import InputFormat
            from docling_core.types.doc import ImageRefMode

            logger.info("Docling DocumentConverter 초기화 중...")

            # PDF 파이프라인 옵션 설정
            pipeline_options = PdfPipelineOptions()
            pipeline_options.do_ocr = self.ocr_enabled
            pipeline_options.do_table_structure = self.table_structure
            pipeline_options.images_scale = 2.0  # 이미지 해상도 스케일

            # DocumentConverter 생성
            self._converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_options=pipeline_options,
                    )
                }
            )
            self._image_ref_mode = ImageRefMode
            logger.info("Docling DocumentConverter 초기화 완료")

        except ImportError as e:
            logger.error(
                f"Docling 라이브러리가 설치되지 않았습니다: {e}\n"
                "설치: pip install docling"
            )
            raise

    def parse_pdf(self, pdf_path: str | Path) -> dict:
        """
        PDF 파일을 Docling으로 파싱합니다.

        Args:
            pdf_path: PDF 파일 경로

        Returns:
            dict: {
                "markdown": str,           # 변환된 Markdown 텍스트
                "document": DoclingDocument, # Docling Document 객체 (후처리용)
                "metadata": dict,          # 문서 메타데이터
            }
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {pdf_path}")

        self._init_converter()

        logger.info(f"PDF 파싱 시작: {pdf_path.name}")

        # Docling으로 PDF 변환
        result = self._converter.convert(str(pdf_path))
        doc = result.document

        # Markdown 변환 (Docling 내장 export 사용)
        markdown_text = doc.export_to_markdown()

        # 문서 메타데이터 수집
        metadata = {
            "source_file": str(pdf_path),
            "file_name": pdf_path.name,
            "num_pages": self._get_page_count(doc),
        }

        logger.info(
            f"PDF 파싱 완료: {pdf_path.name} "
            f"({metadata['num_pages']}페이지, "
            f"{len(markdown_text)}자)"
        )

        return {
            "markdown": markdown_text,
            "document": doc,
            "metadata": metadata,
        }

    def _get_page_count(self, doc) -> int:
        """Docling Document에서 페이지 수를 추출합니다."""
        try:
            # Docling Document 객체에서 페이지 정보 접근
            if hasattr(doc, "pages") and doc.pages:
                return len(doc.pages)
        except Exception:
            pass
        return 0

    def parse_pdf_to_markdown(self, pdf_path: str | Path) -> str:
        """
        PDF를 Markdown 문자열로 변환하는 간편 메서드.

        Args:
            pdf_path: PDF 파일 경로

        Returns:
            변환된 Markdown 문자열
        """
        result = self.parse_pdf(pdf_path)
        return result["markdown"]


class PyMuPDFMetadataExtractor:
    """
    PyMuPDF(fitz)를 사용하여 PDF에서 폰트 메타데이터를 추출합니다.

    Docling은 heading을 단일 레벨(##)로 출력하는 문제가 있으므로,
    PyMuPDF로 각 텍스트 블록의 폰트 크기/굵기 정보를 추출하여
    heading 계층을 복원하는 데 사용합니다.
    """

    def extract_text_blocks_with_metadata(self, pdf_path: str | Path) -> list[dict]:
        """
        PDF에서 모든 텍스트 블록과 폰트 메타데이터를 추출합니다.

        Args:
            pdf_path: PDF 파일 경로

        Returns:
            list[dict]: 각 텍스트 블록의 정보
                - "text": 텍스트 내용
                - "font_size": 폰트 크기 (pt)
                - "is_bold": 굵기 여부
                - "page_num": 페이지 번호
                - "bbox": 바운딩 박스 좌표 (x0, y0, x1, y1)
        """
        import fitz  # PyMuPDF

        pdf_path = Path(pdf_path)
        doc = fitz.open(str(pdf_path))
        text_blocks = []

        for page_num, page in enumerate(doc):
            # 페이지의 텍스트를 딕셔너리 형태로 추출 (span 단위)
            blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

            for block in blocks.get("blocks", []):
                # 텍스트 블록만 처리 (이미지 블록 제외)
                if block.get("type") != 0:
                    continue

                for line in block.get("lines", []):
                    line_text = ""
                    font_sizes = []
                    is_bold = False

                    for span in line.get("spans", []):
                        span_text = span.get("text", "").strip()
                        if not span_text:
                            continue

                        line_text += span.get("text", "")
                        font_sizes.append(span.get("size", 0))

                        # 폰트 이름에 "Bold" 포함 여부 또는 flags 확인
                        font_name = span.get("font", "")
                        flags = span.get("flags", 0)
                        # flags의 bit 4(16)가 bold 플래그
                        if "Bold" in font_name or "bold" in font_name or (flags & 16):
                            is_bold = True

                    line_text = line_text.strip()
                    if not line_text:
                        continue

                    # 해당 라인의 대표 폰트 크기 = 가장 큰 폰트 크기
                    avg_font_size = max(font_sizes) if font_sizes else 0

                    text_blocks.append({
                        "text": line_text,
                        "font_size": round(avg_font_size, 2),
                        "is_bold": is_bold,
                        "page_num": page_num + 1,
                        "bbox": block.get("bbox", (0, 0, 0, 0)),
                    })

        doc.close()
        logger.info(
            f"PyMuPDF 메타데이터 추출 완료: {pdf_path.name} "
            f"({len(text_blocks)}개 텍스트 블록)"
        )
        return text_blocks

    def get_font_statistics(self, text_blocks: list[dict]) -> dict:
        """
        텍스트 블록들에서 폰트 통계를 산출합니다.
        본문 폰트 크기를 추정하여 heading 판단 기준으로 사용합니다.

        Args:
            text_blocks: extract_text_blocks_with_metadata()의 결과

        Returns:
            dict: {
                "body_font_size": float,   # 추정된 본문 폰트 크기
                "font_size_set": set,      # 문서에 등장하는 모든 폰트 크기
                "font_size_counts": dict,  # 각 폰트 크기별 등장 횟수
            }
        """
        from collections import Counter

        if not text_blocks:
            return {"body_font_size": 10.0, "font_size_set": set(), "font_size_counts": {}}

        # 폰트 크기별 등장 횟수 집계
        font_sizes = [b["font_size"] for b in text_blocks if b["font_size"] > 0]
        size_counts = Counter(font_sizes)

        # 가장 많이 등장하는 폰트 크기 = 본문 폰트 크기로 추정
        # (문서에서 본문이 가장 많은 비중을 차지하므로)
        body_font_size = size_counts.most_common(1)[0][0] if size_counts else 10.0

        return {
            "body_font_size": body_font_size,
            "font_size_set": set(font_sizes),
            "font_size_counts": dict(size_counts),
        }
