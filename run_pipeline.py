"""
PDF 처리 파이프라인 실행 스크립트

사용법:
    # 1. data/input_pdfs/ 디렉토리의 모든 PDF 처리
    python run_pipeline.py

    # 2. 특정 PDF 파일 하나만 처리
    python run_pipeline.py --file path/to/document.pdf

    # 3. Vision API 활성화 (이미지 내용 분석, OpenAI 비용 발생)
    python run_pipeline.py --use-vision

    # 4. 벡터 저장소 초기화 후 재처리
    python run_pipeline.py --reset

    # 5. 특정 디렉토리의 PDF 처리
    python run_pipeline.py --dir path/to/pdf_folder
"""
import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(Path(__file__).parent))

from src.pipeline import DocumentPipeline
from src.utils.logger import get_logger

logger = get_logger("run_pipeline")


def main():
    parser = argparse.ArgumentParser(
        description="PDF → Markdown → Embedding 파이프라인",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python run_pipeline.py                          # 모든 PDF 처리
  python run_pipeline.py --file doc.pdf           # 단일 파일 처리
  python run_pipeline.py --use-vision             # Vision API 활성화
  python run_pipeline.py --reset                  # 벡터 DB 초기화 후 처리
        """,
    )
    parser.add_argument(
        "--file", "-f",
        type=str,
        help="처리할 단일 PDF 파일 경로",
    )
    parser.add_argument(
        "--dir", "-d",
        type=str,
        help="PDF 파일이 있는 디렉토리 경로 (기본: data/input_pdfs)",
    )
    parser.add_argument(
        "--use-vision",
        action="store_true",
        help="OpenAI Vision API로 이미지 내용 분석 활성화 (비용 발생)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="벡터 저장소를 초기화(삭제)한 후 처리",
    )

    args = parser.parse_args()

    # 파이프라인 초기화
    pipeline = DocumentPipeline(use_vision_api=args.use_vision)

    # 벡터 저장소 초기화 (--reset 옵션)
    if args.reset:
        logger.info("벡터 저장소 초기화 중...")
        pipeline.vector_store.reset_collection()
        logger.info("벡터 저장소 초기화 완료")

    # 처리 실행
    if args.file:
        # 단일 파일 처리
        pdf_path = Path(args.file)
        if not pdf_path.exists():
            logger.error(f"파일을 찾을 수 없습니다: {pdf_path}")
            sys.exit(1)
        result = pipeline.process_single_pdf(pdf_path)
        logger.info(f"결과: {result}")
    else:
        # 디렉토리 처리
        results = pipeline.process_directory(args.dir)
        if not results:
            logger.warning(
                "처리할 PDF 파일이 없습니다. "
                "data/input_pdfs/ 디렉토리에 PDF 파일을 넣어주세요."
            )

    # 최종 벡터 저장소 상태 출력
    stats = pipeline.get_stats()
    logger.info(f"벡터 저장소 상태: {stats}")


if __name__ == "__main__":
    main()
