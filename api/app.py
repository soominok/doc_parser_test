"""
api/app.py
─────────────────────────────────────────────────────────────────────────────
FastAPI 기반 REST API 서버.
Docker API 모드(APP_MODE=api)에서 uvicorn으로 기동된다.

[제공 엔드포인트]
  GET  /health          - 헬스체크 (Docker healthcheck에서 사용)
  GET  /info            - 서버 정보 및 설정 확인
  POST /parse           - PDF 업로드 → Markdown 텍스트 반환
  POST /parse/file      - PDF 업로드 → Markdown 파일 다운로드
  POST /parse/batch     - 여러 PDF 업로드 → ZIP 파일로 Markdown 묶음 반환
  GET  /jobs/{job_id}   - 비동기 처리 작업 상태 조회
  GET  /jobs/{job_id}/result - 비동기 처리 결과 다운로드

[처리 흐름]
  클라이언트 PDF 업로드
    → /app/input/tmp/{uuid}.pdf 임시 저장
    → process_pdf() 파이프라인 실행
    → Markdown 문자열 반환 또는 파일 스트림
    → 임시 파일 삭제

[비동기 배치 처리]
  대용량 PDF(다수 페이지)는 백그라운드 태스크로 처리하고
  job_id로 상태를 조회할 수 있다.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from loguru import logger

from config import Config
from main import process_pdf

# ─────────────────────────────────────────────
# 앱 초기화
# ─────────────────────────────────────────────

app = FastAPI(
    title="PDF → Markdown 변환 API",
    description="법률 문서, 내규, 매뉴얼 등의 PDF를 Markdown으로 변환하는 API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─────────────────────────────────────────────
# 설정 (환경변수 기반)
# ─────────────────────────────────────────────

UPLOAD_TMP_DIR = Path(os.getenv("UPLOAD_TMP_DIR", "/app/input/tmp"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/app/output"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "100"))  # 100MB 제한
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# 비동기 작업 처리를 위한 스레드풀 (docling은 CPU 집약적)
_executor = ThreadPoolExecutor(max_workers=int(os.getenv("WORKER_THREADS", "2")))

# ─────────────────────────────────────────────
# 비동기 작업 관리 (인메모리, 프로덕션에서는 Redis 권장)
# ─────────────────────────────────────────────

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class ParseJob:
    """비동기 변환 작업 정보"""

    job_id: str
    filename: str
    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result_path: Path | None = None      # 완료된 Markdown 파일 경로
    error: str | None = None
    pages_processed: int = 0


# job_id → ParseJob 저장소 (딕셔너리, 재시작 시 초기화됨)
_jobs: dict[str, ParseJob] = {}


# ─────────────────────────────────────────────
# 유틸 함수
# ─────────────────────────────────────────────

def _build_config(
    no_docling: bool = False,
    max_pages: int | None = None,
    llm_provider: str | None = None,
) -> Config:
    """요청 파라미터로 Config를 동적으로 구성한다."""
    config = Config()
    config.output.output_dir = OUTPUT_DIR

    if max_pages:
        config.parser.max_pages = max_pages

    if llm_provider:
        config.llm.provider = llm_provider  # type: ignore

    return config


async def _save_upload(upload: UploadFile, dest: Path) -> None:
    """업로드된 파일을 비동기로 저장한다."""
    content = await upload.read()

    # 파일 크기 검증
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"파일 크기가 {MAX_FILE_SIZE_MB}MB를 초과합니다.",
        )

    # PDF 시그니처 검증 (%PDF-)
    if not content.startswith(b"%PDF"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="유효한 PDF 파일이 아닙니다.",
        )

    dest.write_bytes(content)


def _run_parse_sync(
    pdf_path: Path,
    output_path: Path | None,
    config: Config,
    use_docling: bool,
) -> Path:
    """
    동기 파싱 함수. ThreadPoolExecutor에서 실행되어
    메인 이벤트 루프를 블로킹하지 않는다.
    """
    return process_pdf(
        pdf_path=pdf_path,
        output_path=output_path,
        config=config,
        use_docling=use_docling,
    )


def _cleanup_tmp(path: Path) -> None:
    """임시 파일을 안전하게 삭제한다."""
    try:
        if path.exists():
            path.unlink()
    except Exception as e:
        logger.warning(f"임시 파일 삭제 실패: {path} - {e}")


# ─────────────────────────────────────────────
# 엔드포인트
# ─────────────────────────────────────────────

@app.get("/health", summary="헬스체크")
async def health_check() -> dict[str, str]:
    """
    Docker healthcheck 및 로드밸런서 probe 용도.
    서버가 정상 작동 중이면 200 OK를 반환한다.
    """
    return {"status": "ok", "timestamp": str(time.time())}


@app.get("/info", summary="서버 정보")
async def server_info() -> dict[str, Any]:
    """현재 서버 설정과 환경 정보를 반환한다."""
    return {
        "version": "1.0.0",
        "max_file_size_mb": MAX_FILE_SIZE_MB,
        "upload_tmp_dir": str(UPLOAD_TMP_DIR),
        "output_dir": str(OUTPUT_DIR),
        "llm_provider": os.getenv("LLM_PROVIDER", "none"),
        "worker_threads": os.getenv("WORKER_THREADS", "2"),
        "active_jobs": len([j for j in _jobs.values() if j.status == JobStatus.RUNNING]),
    }


@app.post(
    "/parse",
    summary="PDF → Markdown 변환 (텍스트 반환)",
    response_class=StreamingResponse,
)
async def parse_pdf_to_text(
    file: UploadFile = File(..., description="변환할 PDF 파일"),
    no_docling: bool = Query(False, description="docling 없이 빠른 처리"),
    max_pages: int | None = Query(None, description="최대 처리 페이지 수"),
) -> StreamingResponse:
    """
    PDF를 업로드하면 변환된 Markdown 텍스트를 스트리밍으로 반환한다.

    - **file**: PDF 파일 (최대 {MAX_FILE_SIZE_MB}MB)
    - **no_docling**: True이면 docling을 건너뛰어 처리 속도가 빨라짐
    - **max_pages**: 테스트 목적으로 처리 페이지 수를 제한할 때 사용

    **예시 (curl):**
    ```bash
    curl -X POST http://localhost:8000/parse \\
         -F "file=@계약서.pdf" \\
         --output 계약서.md
    ```
    """
    UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)

    # 1) 임시 파일에 저장
    tmp_pdf = UPLOAD_TMP_DIR / f"{uuid.uuid4()}.pdf"
    tmp_md = UPLOAD_TMP_DIR / f"{tmp_pdf.stem}.md"

    try:
        await _save_upload(file, tmp_pdf)

        # 2) 동기 파싱을 스레드풀에서 실행 (이벤트 루프 블로킹 방지)
        config = _build_config(no_docling=no_docling, max_pages=max_pages)
        config.output.output_dir = UPLOAD_TMP_DIR  # 임시 디렉토리에 저장

        loop = asyncio.get_event_loop()
        result_path = await loop.run_in_executor(
            _executor,
            _run_parse_sync,
            tmp_pdf,
            tmp_md,
            config,
            not no_docling,
        )

        # 3) 결과를 읽어 스트리밍 반환
        md_text = result_path.read_text(encoding="utf-8")
        original_stem = Path(file.filename or "output").stem

        return StreamingResponse(
            io.StringIO(md_text),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{original_stem}.md"',
                "X-Original-Filename": file.filename or "",
                "X-Pages-Processed": "unknown",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"파싱 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"파싱 중 오류가 발생했습니다: {str(e)}",
        )
    finally:
        # 4) 임시 파일 정리
        _cleanup_tmp(tmp_pdf)
        _cleanup_tmp(tmp_md)


@app.post(
    "/parse/file",
    summary="PDF → Markdown 변환 (파일 저장 후 경로 반환)",
)
async def parse_pdf_to_file(
    file: UploadFile = File(...),
    no_docling: bool = Query(False),
    max_pages: int | None = Query(None),
) -> JSONResponse:
    """
    PDF를 업로드하면 서버의 OUTPUT_DIR에 Markdown 파일을 저장하고
    저장된 파일 경로를 반환한다.

    배치 파이프라인에서 컨테이너 내부 파일 시스템을 활용할 때 사용한다.
    """
    UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    tmp_pdf = UPLOAD_TMP_DIR / f"{uuid.uuid4()}.pdf"
    original_stem = Path(file.filename or "output").stem
    out_md = OUTPUT_DIR / f"{original_stem}_parsed.md"

    try:
        await _save_upload(file, tmp_pdf)

        config = _build_config(no_docling=no_docling, max_pages=max_pages)
        config.output.output_dir = OUTPUT_DIR

        loop = asyncio.get_event_loop()
        result_path = await loop.run_in_executor(
            _executor,
            _run_parse_sync,
            tmp_pdf,
            out_md,
            config,
            not no_docling,
        )

        return JSONResponse({
            "status": "success",
            "output_path": str(result_path),
            "filename": result_path.name,
            "size_bytes": result_path.stat().st_size,
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"파싱 오류: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
    finally:
        _cleanup_tmp(tmp_pdf)


@app.post(
    "/parse/batch",
    summary="여러 PDF → ZIP 파일로 Markdown 묶음 반환",
)
async def parse_batch(
    files: list[UploadFile] = File(..., description="변환할 PDF 파일 목록"),
    no_docling: bool = Query(False),
    max_pages: int | None = Query(None),
) -> StreamingResponse:
    """
    여러 PDF를 업로드하면 모두 변환 후 ZIP 파일로 묶어 반환한다.

    **예시 (curl):**
    ```bash
    curl -X POST http://localhost:8000/parse/batch \\
         -F "files=@문서1.pdf" \\
         -F "files=@문서2.pdf" \\
         --output 결과.zip
    ```
    """
    if len(files) > 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="한 번에 최대 20개의 파일만 처리할 수 있습니다.",
        )

    UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    config = _build_config(no_docling=no_docling, max_pages=max_pages)
    config.output.output_dir = UPLOAD_TMP_DIR

    results: list[tuple[str, bytes]] = []  # (파일명, 내용)
    errors: list[str] = []
    tmp_files: list[Path] = []

    try:
        loop = asyncio.get_event_loop()

        for upload in files:
            tmp_pdf = UPLOAD_TMP_DIR / f"{uuid.uuid4()}.pdf"
            tmp_md = UPLOAD_TMP_DIR / f"{tmp_pdf.stem}.md"
            tmp_files.extend([tmp_pdf, tmp_md])
            original_stem = Path(upload.filename or "output").stem

            try:
                await _save_upload(upload, tmp_pdf)
                result_path = await loop.run_in_executor(
                    _executor,
                    _run_parse_sync,
                    tmp_pdf,
                    tmp_md,
                    config,
                    not no_docling,
                )
                results.append((f"{original_stem}.md", result_path.read_bytes()))

            except Exception as e:
                errors.append(f"{upload.filename}: {str(e)}")
                logger.error(f"배치 파싱 오류 ({upload.filename}): {e}")

        # ZIP 파일 생성 (메모리 내)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname, content in results:
                zf.writestr(fname, content)
            # 오류 목록도 ZIP에 포함
            if errors:
                zf.writestr("_errors.txt", "\n".join(errors))

        zip_buffer.seek(0)

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="parsed_markdowns.zip"',
                "X-Success-Count": str(len(results)),
                "X-Error-Count": str(len(errors)),
            },
        )

    finally:
        for f in tmp_files:
            _cleanup_tmp(f)


# ─────────────────────────────────────────────
# 비동기 작업 처리 (대용량 PDF용)
# ─────────────────────────────────────────────

def _background_parse(job_id: str, pdf_path: Path, config: Config, use_docling: bool) -> None:
    """백그라운드에서 파싱을 실행하고 job 상태를 업데이트한다."""
    job = _jobs.get(job_id)
    if not job:
        return

    job.status = JobStatus.RUNNING
    try:
        out_path = OUTPUT_DIR / f"{pdf_path.stem}_parsed.md"
        result_path = process_pdf(
            pdf_path=pdf_path,
            output_path=out_path,
            config=config,
            use_docling=use_docling,
        )
        job.status = JobStatus.DONE
        job.result_path = result_path
        job.finished_at = time.time()
        logger.info(f"[job:{job_id}] 완료: {result_path}")
    except Exception as e:
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.finished_at = time.time()
        logger.error(f"[job:{job_id}] 실패: {e}")
    finally:
        _cleanup_tmp(pdf_path)


@app.post("/jobs", summary="비동기 변환 작업 등록", status_code=202)
async def create_parse_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    no_docling: bool = Query(False),
    max_pages: int | None = Query(None),
) -> JSONResponse:
    """
    PDF를 업로드하고 백그라운드에서 비동기 변환을 시작한다.
    job_id를 즉시 반환하고, GET /jobs/{job_id}로 상태를 확인한다.

    대용량(100페이지 이상) PDF 처리 시 타임아웃 방지를 위해 사용한다.
    """
    UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    job_id = str(uuid.uuid4())
    tmp_pdf = UPLOAD_TMP_DIR / f"{job_id}.pdf"

    await _save_upload(file, tmp_pdf)

    config = _build_config(no_docling=no_docling, max_pages=max_pages)

    job = ParseJob(job_id=job_id, filename=file.filename or "unknown.pdf")
    _jobs[job_id] = job

    background_tasks.add_task(
        _background_parse, job_id, tmp_pdf, config, not no_docling
    )

    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": JobStatus.PENDING,
            "message": f"GET /jobs/{job_id} 로 상태를 확인하세요.",
        },
    )


@app.get("/jobs/{job_id}", summary="비동기 작업 상태 조회")
async def get_job_status(job_id: str) -> dict[str, Any]:
    """job_id에 해당하는 변환 작업의 현재 상태를 반환한다."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job_id '{job_id}'를 찾을 수 없습니다.")

    elapsed = (
        (job.finished_at or time.time()) - job.created_at
    )

    return {
        "job_id": job.job_id,
        "filename": job.filename,
        "status": job.status,
        "elapsed_seconds": round(elapsed, 1),
        "result_path": str(job.result_path) if job.result_path else None,
        "error": job.error,
    }


@app.get("/jobs/{job_id}/result", summary="비동기 작업 결과 다운로드")
async def download_job_result(job_id: str) -> FileResponse:
    """완료된 작업의 Markdown 파일을 다운로드한다."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job_id '{job_id}'를 찾을 수 없습니다.")

    if job.status != JobStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail=f"작업이 아직 완료되지 않았습니다. 현재 상태: {job.status}",
        )

    if not job.result_path or not job.result_path.exists():
        raise HTTPException(status_code=404, detail="결과 파일을 찾을 수 없습니다.")

    return FileResponse(
        path=str(job.result_path),
        media_type="text/markdown",
        filename=job.result_path.name,
    )
