"""
전체 파이프라인 오케스트레이터

PDF → Markdown → Heading 복원 → 이미지 분석 → Chunking → Embedding → 저장

이 모듈이 전체 처리 흐름을 조율합니다.
개별 모듈을 순서대로 호출하여 PDF를 RAG에 사용 가능한 형태로 변환합니다.

실행 흐름:
  ┌─────────────────────────────────────────────────────────────┐
  │  1. PDF 입력                                                │
  │     ↓                                                       │
  │  2. Docling: PDF → Markdown (구조 분석, 표 추출)             │
  │     ↓                                                       │
  │  3. PyMuPDF: 폰트 메타데이터 추출                            │
  │     ↓                                                       │
  │  4. HeadingProcessor: heading 계층 복원 (패턴+폰트 분석)     │
  │     ↓                                                       │
  │  5. ImageExtractor: 이미지 추출 + Vision API 분석 (선택)     │
  │     ↓                                                       │
  │  6. Markdown 파일 저장                                       │
  │     ↓                                                       │
  │  7. MarkdownChunker: heading 기반 청크 분할                  │
  │     ↓                                                       │
  │  8. VectorStoreManager: 임베딩 + ChromaDB 저장               │
  └─────────────────────────────────────────────────────────────┘
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import (
    INPUT_PDF_DIR,
    OUTPUT_MD_DIR,
    IMAGES_DIR,
    DOCLING_CONFIG,
    VISION_MODEL,
)
from src.parsers.docling_parser import DoclingParser, PyMuPDFMetadataExtractor
from src.parsers.image_extractor import ImageExtractor
from src.processing.heading_processor import HeadingProcessor
from src.processing.chunker import MarkdownChunker
from src.embedding.vector_store import VectorStoreManager
from src.utils.logger import get_logger

logger = get_logger(__name__)


class DocumentPipeline:
    """
    PDF 문서 처리 파이프라인.

    하나의 PDF 또는 디렉토리 내 모든 PDF를 처리합니다.
    """

    def __init__(self, use_vision_api: bool = False):
        """
        Args:
            use_vision_api: 이미지 Vision API 분석 사용 여부
                True: OpenAI Vision API로 이미지 내용 분석 (비용 발생)
                False: 이미지 추출만 하고 간단한 플레이스홀더만 삽입
        """
        self.use_vision_api = use_vision_api

        # 각 처리 모듈 초기화
        self.docling_parser = DoclingParser(
            ocr_enabled=DOCLING_CONFIG["do_ocr"],
            table_structure=DOCLING_CONFIG["do_table_structure"],
        )
        self.metadata_extractor = PyMuPDFMetadataExtractor()
        self.heading_processor = HeadingProcessor()
        self.image_extractor = ImageExtractor(
            images_dir=IMAGES_DIR,
            vision_model=VISION_MODEL,
        )
        self.chunker = MarkdownChunker()
        self.vector_store = VectorStoreManager()

    def process_single_pdf(self, pdf_path: str | Path) -> dict:
        """
        단일 PDF 파일을 전체 파이프라인으로 처리합니다.

        Args:
            pdf_path: PDF 파일 경로

        Returns:
            dict: {
                "markdown_path": str,    # 저장된 Markdown 파일 경로
                "num_chunks": int,       # 생성된 청크 수
                "num_images": int,       # 추출된 이미지 수
                "markdown_text": str,    # 최종 Markdown 텍스트
            }
        """
        pdf_path = Path(pdf_path)
        logger.info(f"{'='*60}")
        logger.info(f"파이프라인 시작: {pdf_path.name}")
        logger.info(f"{'='*60}")

        # ──────────────────────────────────────────────────
        # Step 1: Docling으로 PDF → Markdown 변환
        # ──────────────────────────────────────────────────
        logger.info("[Step 1/6] Docling PDF 파싱...")
        parse_result = self.docling_parser.parse_pdf(pdf_path)
        markdown_text = parse_result["markdown"]
        logger.info(f"  → Markdown 변환 완료: {len(markdown_text)}자")

        # ──────────────────────────────────────────────────
        # Step 2: PyMuPDF로 폰트 메타데이터 추출
        # ──────────────────────────────────────────────────
        logger.info("[Step 2/6] PyMuPDF 폰트 메타데이터 추출...")
        text_blocks = self.metadata_extractor.extract_text_blocks_with_metadata(pdf_path)
        font_stats = self.metadata_extractor.get_font_statistics(text_blocks)
        logger.info(
            f"  → {len(text_blocks)}개 블록, "
            f"본문 폰트 크기: {font_stats['body_font_size']}pt"
        )

        # ──────────────────────────────────────────────────
        # Step 3: Heading 계층 구조 복원
        # ──────────────────────────────────────────────────
        logger.info("[Step 3/6] Heading 계층 복원...")
        markdown_text = self.heading_processor.process_markdown_with_context_analysis(
            markdown_text=markdown_text,
            text_blocks=text_blocks,
            font_stats=font_stats,
        )

        # ──────────────────────────────────────────────────
        # Step 4: 이미지 추출 및 분석
        # ──────────────────────────────────────────────────
        logger.info("[Step 4/6] 이미지 추출 및 분석...")
        image_results = self.image_extractor.process_images_for_document(
            pdf_path=pdf_path,
            use_vision_api=self.use_vision_api,
        )
        num_images = len(image_results)

        if image_results:
            markdown_text = self.image_extractor.inject_image_descriptions_into_markdown(
                markdown_text=markdown_text,
                image_descriptions=image_results,
            )
            logger.info(f"  → {num_images}개 이미지 설명 삽입 완료")
        else:
            logger.info("  → 추출된 이미지 없음")

        # ──────────────────────────────────────────────────
        # Step 5: Markdown 파일 저장
        # ──────────────────────────────────────────────────
        logger.info("[Step 5/6] Markdown 파일 저장...")
        md_filename = pdf_path.stem + ".md"
        md_path = OUTPUT_MD_DIR / md_filename
        md_path.write_text(markdown_text, encoding="utf-8")
        logger.info(f"  → 저장: {md_path}")

        # ──────────────────────────────────────────────────
        # Step 6: Chunking + Embedding + 벡터 저장
        # ──────────────────────────────────────────────────
        logger.info("[Step 6/6] Chunking + Embedding...")
        chunks = self.chunker.chunk_markdown(
            markdown_text=markdown_text,
            source_file=str(pdf_path),
        )
        num_stored = self.vector_store.add_chunks(chunks)
        logger.info(f"  → {num_stored}개 청크 벡터 저장 완료")

        logger.info(f"{'='*60}")
        logger.info(f"파이프라인 완료: {pdf_path.name}")
        logger.info(f"{'='*60}\n")

        return {
            "markdown_path": str(md_path),
            "num_chunks": num_stored,
            "num_images": num_images,
            "markdown_text": markdown_text,
        }

    def process_directory(self, pdf_dir: str | Path = None) -> list[dict]:
        """
        디렉토리 내 모든 PDF 파일을 처리합니다.

        Args:
            pdf_dir: PDF 파일이 있는 디렉토리 (기본: data/input_pdfs)

        Returns:
            list[dict]: 각 PDF의 처리 결과 목록
        """
        pdf_dir = Path(pdf_dir or INPUT_PDF_DIR)
        pdf_files = sorted(pdf_dir.glob("*.pdf"))

        if not pdf_files:
            logger.warning(f"PDF 파일을 찾을 수 없습니다: {pdf_dir}")
            return []

        logger.info(f"총 {len(pdf_files)}개 PDF 파일 발견: {pdf_dir}")

        results = []
        for i, pdf_path in enumerate(pdf_files, 1):
            logger.info(f"\n[{i}/{len(pdf_files)}] 처리 중: {pdf_path.name}")
            try:
                result = self.process_single_pdf(pdf_path)
                results.append(result)
            except Exception as e:
                logger.error(f"PDF 처리 실패: {pdf_path.name} → {e}")
                results.append({
                    "markdown_path": None,
                    "num_chunks": 0,
                    "num_images": 0,
                    "error": str(e),
                })

        # 최종 요약
        total_chunks = sum(r.get("num_chunks", 0) for r in results)
        total_images = sum(r.get("num_images", 0) for r in results)
        success_count = sum(1 for r in results if r.get("markdown_path"))

        logger.info(f"\n{'='*60}")
        logger.info(f"전체 처리 완료: {success_count}/{len(pdf_files)} 성공")
        logger.info(f"총 청크: {total_chunks}개, 총 이미지: {total_images}개")
        logger.info(f"{'='*60}")

        return results

    def get_stats(self) -> dict:
        """현재 벡터 저장소 상태를 반환합니다."""
        return self.vector_store.get_collection_stats()
