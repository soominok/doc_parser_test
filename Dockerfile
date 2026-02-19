# ────────────────────────────────────────────────────────────────
# PDF → Markdown 하이브리드 파서 - Dockerfile
# ────────────────────────────────────────────────────────────────
#
# [빌드 방법]
#   docker build -t pdf-parser .
#
# [실행 방법]
#   docker run -p 7860:7860 pdf-parser
#   # 브라우저: http://localhost:7860
#
# [변환 결과 파일 저장 (호스트 마운트)]
#   docker run -p 7860:7860 -v $(pwd)/output:/app/output pdf-parser
#
# [선택: 모델 캐시 유지 (재다운로드 방지)]
#   docker run -p 7860:7860 -v model_cache:/root/.cache pdf-parser
# ────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# ── 메타데이터 ───────────────────────────────────────────────────
LABEL maintainer="pdf-parser"
LABEL description="Hybrid PDF to Markdown Parser (Docling + PyMuPDF + pdfplumber)"

# ── 시스템 의존성 ────────────────────────────────────────────────
# libgl1 / libglib2.0-0 : OpenCV 실행에 필요 (docling 내부 사용)
# libgomp1              : PyTorch OpenMP 병렬 처리
# curl                  : 헬스체크용
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── 작업 디렉토리 ────────────────────────────────────────────────
WORKDIR /app

# ── Python 패키지 설치 ───────────────────────────────────────────
# requirements.txt를 먼저 복사하면 소스 변경 시 캐시 레이어 재활용 가능
COPY requirements.txt .

# pip 업그레이드 후 패키지 설치
# --no-cache-dir : 이미지 크기 최소화
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── 소스 코드 복사 ───────────────────────────────────────────────
COPY pdf_parser.py .

# ── 출력 디렉토리 생성 ───────────────────────────────────────────
# 변환된 .md 파일이 저장되는 경로 (호스트 볼륨 마운트 가능)
RUN mkdir -p /app/output

# ── 환경변수 기본값 ──────────────────────────────────────────────
# DOCKER_ENV  : pdf_parser.py 내에서 Docker 모드 감지에 사용
#               → inbrowser=False, 콘솔에 접속 URL 출력
# GRADIO_HOST : Gradio 바인딩 호스트 (컨테이너 외부 접근 위해 0.0.0.0 필수)
# GRADIO_PORT : Gradio 바인딩 포트
# OUTPUT_DIR  : 변환 결과 .md 파일 저장 경로
ENV DOCKER_ENV=true \
    GRADIO_HOST=0.0.0.0 \
    GRADIO_PORT=7860 \
    OUTPUT_DIR=/app/output

# ── Gradio 포트 노출 ─────────────────────────────────────────────
EXPOSE 7860

# ── 헬스체크 ─────────────────────────────────────────────────────
# 30초 후부터 10초 간격으로 서버 응답 확인
# Gradio 첫 실행 시 모델 다운로드로 시간이 걸릴 수 있어 start-period 넉넉하게 설정
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:7860/ || exit 1

# ── 실행 ─────────────────────────────────────────────────────────
CMD ["python", "pdf_parser.py"]
