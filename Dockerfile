# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile  (Multi-stage build)
#
# Stage 1 [builder] : Python 의존 패키지 빌드
# Stage 2 [runtime] : 실행에 필요한 파일만 담은 경량 최종 이미지
#
# 빌드:  docker build -t pdf-parser .
# 실행:  docker-compose up
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: 의존성 빌드 ──────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# 시스템 패키지 설치 (빌드 도구 + 런타임 공통)
# - build-essential : C 확장 모듈 컴파일용 (pdfminer, Pillow 등)
# - libgl1 libglib2.0-0 : OpenCV (docling 내부 의존)
# - libgomp1 : PyTorch OpenMP (docling 딥러닝 모델)
# - poppler-utils : PDF 처리 유틸
# - tesseract-ocr + 언어팩 : docling OCR 백엔드
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    libffi-dev \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-kor \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# requirements를 먼저 복사하여 레이어 캐시 최대 활용
# (소스 코드가 바뀌어도 pip install 레이어는 재사용됨)
COPY requirements.txt .

# pip 업그레이드 후 패키지 설치
# --no-cache-dir : 빌더 이미지 용량 절감
# --prefix : /install 에 설치하여 런타임 스테이지로 복사하기 쉽게 분리
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: 런타임 이미지 ────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="pdf-parser"
LABEL description="PDF → Markdown 변환 파이프라인 (docling + pdfplumber + PyMuPDF)"

# 런타임에 필요한 시스템 패키지만 설치 (빌드 도구 제외)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-kor \
    tesseract-ocr-eng \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 빌더 스테이지에서 설치된 Python 패키지를 복사
COPY --from=builder /install /usr/local

WORKDIR /app

# ── 디렉토리 구조 ────────────────────────────────────────────────────────────
# /app/input    : 컨테이너에서 읽을 PDF 파일 (호스트 볼륨 마운트)
# /app/output   : 변환된 Markdown 파일 출력 (호스트 볼륨 마운트)
# /app/models   : docling 딥러닝 모델 캐시 (영구 볼륨으로 유지 권장)
# /app/debug    : 중간 결과 JSON (디버깅용)
RUN mkdir -p /app/input /app/output /app/models /app/debug

# 소스 코드 복사
COPY . .

# ── docling 모델 캐시 경로 설정 ──────────────────────────────────────────────
# docling은 HuggingFace Hub에서 모델을 다운로드한다.
# 컨테이너 재시작 시 재다운로드를 방지하기 위해 /app/models 에 캐시.
ENV HF_HOME=/app/models
ENV DOCLING_ARTIFACTS_PATH=/app/models
# HuggingFace Hub 오프라인 모드 (모델이 미리 캐시되어 있을 때 네트워크 차단)
# 첫 실행 시 이 값을 "0"으로 두거나 제거하세요.
ENV TRANSFORMERS_OFFLINE=0
ENV HF_DATASETS_OFFLINE=0

# ── 기본 환경변수 ────────────────────────────────────────────────────────────
ENV INPUT_DIR=/app/input
ENV OUTPUT_DIR=/app/output
ENV DEBUG_DIR=/app/debug
ENV LLM_PROVIDER=none
ENV LOG_LEVEL=INFO
# API 서버 포트
ENV API_PORT=8000
# 실행 모드: "cli" (배치 처리) | "api" (REST API 서버)
ENV APP_MODE=cli

# Python 출력 버퍼링 비활성화 (로그가 실시간으로 보이도록)
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# entrypoint 실행 권한 부여
RUN chmod +x /app/docker-entrypoint.sh

# API 서버 포트 노출
EXPOSE 8000

# 헬스체크: API 모드일 때 서버 상태 확인
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${API_PORT}/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
