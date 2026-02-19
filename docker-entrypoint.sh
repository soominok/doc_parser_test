#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# docker-entrypoint.sh
#
# 컨테이너 시작 시 APP_MODE 환경변수에 따라 두 가지 모드로 분기한다.
#
#   APP_MODE=cli  → main.py를 CLI로 실행 (배치 변환)
#   APP_MODE=api  → uvicorn으로 FastAPI 서버 기동
#
# 추가 인자는 그대로 main.py 또는 uvicorn에 전달된다.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── 컬러 로그 함수 ────────────────────────────────────────────────────────────
log_info()  { echo -e "\033[0;32m[INFO]\033[0m  $*"; }
log_warn()  { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
log_error() { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; }

# ── 디렉토리 초기화 ──────────────────────────────────────────────────────────
# 볼륨 마운트된 디렉토리가 없으면 생성
mkdir -p "${INPUT_DIR:-/app/input}" \
         "${OUTPUT_DIR:-/app/output}" \
         "${DEBUG_DIR:-/app/debug}" \
         "${HF_HOME:-/app/models}"

log_info "=== PDF → Markdown 변환 파이프라인 ==="
log_info "APP_MODE      : ${APP_MODE:-cli}"
log_info "INPUT_DIR     : ${INPUT_DIR:-/app/input}"
log_info "OUTPUT_DIR    : ${OUTPUT_DIR:-/app/output}"
log_info "LLM_PROVIDER  : ${LLM_PROVIDER:-none}"
log_info "HF_HOME       : ${HF_HOME:-/app/models}"

# ── docling 모델 사전 다운로드 확인 ────────────────────────────────────────────
# 모델이 캐시에 없으면 최초 1회 다운로드 (시간 소요: 수 분)
# SKIP_MODEL_DOWNLOAD=true 로 설정하면 건너뜀 (오프라인 환경)
if [ "${SKIP_MODEL_DOWNLOAD:-false}" = "false" ]; then
    log_info "docling 모델 캐시 확인 중..."
    python -c "
import os
cache = os.environ.get('DOCLING_ARTIFACTS_PATH', '/app/models')
# 모델 디렉토리가 비어 있으면 다운로드 트리거
import pathlib
model_dir = pathlib.Path(cache)
existing = list(model_dir.rglob('*.safetensors'))
if not existing:
    print('[INFO] 모델 캐시 없음. 첫 실행 시 자동 다운로드됩니다 (~1GB).')
else:
    print(f'[INFO] 모델 캐시 확인됨: {len(existing)}개 파일')
" 2>/dev/null || true
fi

# ── 모드 분기 ────────────────────────────────────────────────────────────────

case "${APP_MODE:-cli}" in

  # ── CLI 배치 모드 ────────────────────────────────────────────────────────
  "cli")
    log_info "CLI 배치 모드로 실행합니다."

    # 추가 인자가 없으면 INPUT_DIR 내의 모든 PDF를 처리
    if [ $# -eq 0 ]; then
      log_info "INPUT_DIR(${INPUT_DIR}) 내 모든 PDF를 변환합니다."
      exec python /app/main.py \
        --input  "${INPUT_DIR:-/app/input}" \
        --output "${OUTPUT_DIR:-/app/output}"
    else
      # 추가 인자를 그대로 main.py에 전달
      # 예: docker-compose run --rm parser --input /app/input/문서.pdf --no-docling
      exec python /app/main.py "$@"
    fi
    ;;

  # ── API 서버 모드 ────────────────────────────────────────────────────────
  "api")
    log_info "REST API 서버 모드로 실행합니다. (포트: ${API_PORT:-8000})"

    # 업로드 임시 디렉토리 생성
    mkdir -p "${UPLOAD_TMP_DIR:-/app/input/tmp}"

    exec uvicorn api.app:app \
      --host 0.0.0.0 \
      --port "${API_PORT:-8000}" \
      --workers 1 \
      --log-level "${LOG_LEVEL:-info}" \
      "$@"
    ;;

  *)
    log_error "알 수 없는 APP_MODE: '${APP_MODE}'. 'cli' 또는 'api'를 지정하세요."
    exit 1
    ;;

esac
