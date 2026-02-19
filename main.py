"""
main.py
─────────────────────────────────────────────────────────────────────────────
PDF → Markdown 변환 파이프라인 메인 진입점.

[파이프라인 흐름]
  ┌──────────┐   ┌─────────────┐   ┌──────────────┐
  │  PDF 입력 │──▶│  파서 3종   │──▶│  HeadingDet  │
  └──────────┘   │ · PyMuPDF   │   │  TableProc   │
                 │ · pdfplumber │   │  Merger      │
                 │ · docling    │   └──────┬───────┘
                 └─────────────┘          │
                                          ▼
                              ┌───────────────────────┐
                              │  LLM Enhancer (선택적) │
                              └──────────┬────────────┘
                                         │
                                         ▼
                              ┌──────────────────────┐
                              │  MarkdownExporter     │
                              └──────────────────────┘
                                         │
                                         ▼
                                   .md 파일 출력

[사용법 - 로컬]
  python main.py --input document.pdf
  python main.py --input ./pdfs/ --output ./output/
  python main.py --input document.pdf --no-docling
  python main.py --input document.pdf --debug

[사용법 - Docker CLI 모드]
  # INPUT_DIR 전체 처리 (entrypoint가 자동으로 --input 설정)
  docker-compose run --rm parser

  # 특정 파일만 처리
  docker-compose run --rm parser --input /app/input/계약서.pdf

  # 옵션 전달
  docker-compose run --rm parser --input /app/input/ --no-docling --verbose

[사용법 - Docker API 모드]
  docker-compose up api
  curl -X POST http://localhost:8000/parse -F "file=@문서.pdf" --output 문서.md
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from loguru import logger
from tqdm import tqdm

from config import Config, DEFAULT_CONFIG
from exporters.markdown_exporter import MarkdownExporter
from llm.enhancer import LLMEnhancer
from parsers import docling_parser, pdfplumber_parser, pymupdf_parser
from processors.merger import DocumentMerger


# ─────────────────────────────────────────────
# 로거 설정
# ─────────────────────────────────────────────

def _setup_logger(verbose: bool = False) -> None:
    """loguru 로거를 설정한다."""
    logger.remove()  # 기본 핸들러 제거
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True,
    )


# ─────────────────────────────────────────────
# 단일 PDF 처리 함수
# ─────────────────────────────────────────────

def process_pdf(
    pdf_path: Path,
    output_path: Path | None = None,
    config: Config = DEFAULT_CONFIG,
    use_docling: bool = True,
) -> Path:
    """
    PDF 파일 한 개를 Markdown으로 변환하는 전체 파이프라인.

    Parameters
    ----------
    pdf_path : Path
        입력 PDF 경로
    output_path : Path | None
        출력 Markdown 경로. None이면 자동 결정.
    config : Config
        전역 설정
    use_docling : bool
        docling 파서 사용 여부 (False면 폰트 기반만 사용, 빠름)

    Returns
    -------
    Path
        생성된 Markdown 파일 경로
    """
    start = time.time()
    logger.info(f"처리 시작: {pdf_path.name}")

    # ── Step 1: PyMuPDF 파싱 (폰트 메타데이터) ──
    logger.info("  [1/4] PyMuPDF 파싱 중...")
    pymupdf_result = pymupdf_parser.parse(pdf_path, config)
    logger.debug(
        f"       {pymupdf_result.page_count}페이지, "
        f"폰트 크기 종류: {len(pymupdf_result.font_size_histogram)}종"
    )

    # ── Step 2: pdfplumber 파싱 (정확한 텍스트 + 표) ──
    logger.info("  [2/4] pdfplumber 파싱 중...")
    pdfplumber_result = pdfplumber_parser.parse(pdf_path, config)
    total_tables = sum(len(t) for t in pdfplumber_result.tables.values())
    logger.debug(f"       표 {total_tables}개 감지")

    # ── Step 3: docling 파싱 (문서 구조) ──────────
    docling_result = None
    if use_docling:
        logger.info("  [3/4] docling 파싱 중 (시간 소요)...")
        try:
            docling_result = docling_parser.parse(pdf_path, config)
            heading_count = sum(
                1 for e in docling_result.elements if e.is_heading
            )
            logger.debug(f"       요소 {len(docling_result.elements)}개, heading {heading_count}개")
        except Exception as e:
            logger.warning(f"       docling 파싱 실패: {e}. 폰트 기반만 사용합니다.")
            docling_result = None
    else:
        logger.info("  [3/4] docling 건너뜀 (--no-docling 옵션)")

    # ── Step 4: 병합 (Heading 감지 + 표 처리 + 텍스트 정확도 개선) ──
    logger.info("  [4/4] 결과 병합 중...")
    merger = DocumentMerger(
        pymupdf_result=pymupdf_result,
        pdfplumber_result=pdfplumber_result,
        docling_result=docling_result,
        config=config,
    )
    merged_doc = merger.merge()

    # ── 선택적: LLM 보강 ──────────────────────────
    if config.llm_enabled:
        logger.info("  [+] LLM 보강 중...")
        enhancer = LLMEnhancer(config)
        merged_doc.elements = enhancer.enhance(merged_doc.elements, str(pdf_path))

    # ── 중간 결과 저장 (디버깅) ──────────────────
    if config.output.save_intermediate:
        _save_intermediate(merged_doc, config)

    # ── Markdown 출력 ──────────────────────────────
    exporter = MarkdownExporter(config)
    result_path = exporter.to_file(merged_doc, output_path)

    elapsed = time.time() - start
    logger.info(f"완료: {result_path.name} ({elapsed:.1f}초)")
    return result_path


def _save_intermediate(merged_doc, config: Config) -> None:
    """병합 결과를 JSON으로 저장한다 (디버깅용)."""
    debug_dir = config.output.intermediate_dir
    debug_dir.mkdir(parents=True, exist_ok=True)

    stem = Path(merged_doc.pdf_path).stem
    debug_path = debug_dir / f"{stem}_intermediate.json"

    data = {
        "pdf_path": merged_doc.pdf_path,
        "page_count": merged_doc.page_count,
        "elements": [
            {
                "kind": e.kind,
                "level": e.level,
                "text": e.text[:200],  # 너무 길면 잘라서 저장
                "page": e.page_number,
                "confidence": e.confidence,
                "source": e.source,
            }
            for e in merged_doc.elements
        ],
    }

    with open(debug_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.debug(f"중간 결과 저장: {debug_path}")


# ─────────────────────────────────────────────
# CLI 인자 파싱
# ─────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PDF → Markdown 변환기 (docling + pdfplumber + PyMuPDF 통합)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
로컬 실행 예시:
  python main.py --input contract.pdf
  python main.py --input ./docs/ --output ./output/
  python main.py --input manual.pdf --no-docling
  python main.py --input report.pdf --debug --verbose

Docker CLI 모드 예시:
  docker-compose run --rm parser
  docker-compose run --rm parser --input /app/input/문서.pdf
  docker-compose run --rm parser --input /app/input/ --no-docling

Docker API 모드:
  docker-compose up api
  curl -X POST http://localhost:8000/parse -F "file=@문서.pdf" --output 문서.md
  curl -X POST http://localhost:8000/parse/batch -F "files=@a.pdf" -F "files=@b.pdf"
        """,
    )

    parser.add_argument(
        "--input", "-i",
        required=True,
        help="입력 PDF 파일 또는 PDF가 있는 디렉토리 경로",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="출력 경로 (파일 또는 디렉토리). 기본값: 입력과 같은 디렉토리",
    )
    parser.add_argument(
        "--no-docling",
        action="store_true",
        help="docling 파서를 사용하지 않음 (빠르지만 구조 인식 정확도 낮음)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="처리할 최대 페이지 수 (테스트용)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="중간 결과(JSON)를 ./debug_output/에 저장",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="상세 로그 출력",
    )

    return parser.parse_args()


# ─────────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    _setup_logger(args.verbose)

    # 설정 구성
    config = Config()
    if args.max_pages:
        config.parser.max_pages = args.max_pages
    if args.debug:
        config.output.save_intermediate = True

    input_path = Path(args.input)

    # ── 입력이 디렉토리인 경우: 하위 PDF 일괄 처리 ──
    if input_path.is_dir():
        pdf_files = sorted(input_path.glob("**/*.pdf"))
        if not pdf_files:
            logger.error(f"디렉토리에서 PDF 파일을 찾을 수 없습니다: {input_path}")
            sys.exit(1)

        output_dir = Path(args.output) if args.output else None
        if output_dir:
            config.output.output_dir = output_dir

        logger.info(f"총 {len(pdf_files)}개 PDF 처리 시작")

        success_count = 0
        fail_count = 0

        for pdf_file in tqdm(pdf_files, desc="PDF 변환 중", unit="파일"):
            try:
                out_path = None
                if output_dir:
                    # 입력 디렉토리 구조를 출력 디렉토리에서도 유지
                    rel = pdf_file.relative_to(input_path)
                    out_path = output_dir / rel.with_suffix(f"{config.output.suffix}.md")

                process_pdf(
                    pdf_path=pdf_file,
                    output_path=out_path,
                    config=config,
                    use_docling=not args.no_docling,
                )
                success_count += 1

            except Exception as e:
                logger.error(f"처리 실패: {pdf_file.name} - {e}")
                fail_count += 1

        logger.info(f"\n처리 완료: 성공 {success_count}개, 실패 {fail_count}개")

    # ── 입력이 단일 파일인 경우 ──────────────────
    elif input_path.is_file() and input_path.suffix.lower() == ".pdf":
        output_path = Path(args.output) if args.output else None

        try:
            process_pdf(
                pdf_path=input_path,
                output_path=output_path,
                config=config,
                use_docling=not args.no_docling,
            )
        except Exception as e:
            logger.error(f"처리 실패: {e}")
            sys.exit(1)

    else:
        logger.error(f"유효하지 않은 입력 경로: {input_path}")
        sys.exit(1)


if __name__ == "__main__":
    main()
