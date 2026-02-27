# ─────────────────────────────────────────────
#  Multi-format Document Parser
#  지원: PDF, DOCX, XLSX, HWP/HWPX, PPT/PPTX
# ─────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# ── 시스템 패키지 ────────────────────────────────────────────────────────────
# LibreOffice : HWP / 구형 PPT(.ppt) 변환 용도
# libgl1, libglib2.0-0 : Pillow(pdfplumber 의존) 런타임 라이브러리
# fonts-nanum : 한글 폰트 (LibreOffice 변환 품질 향상)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice \
        libgl1 \
        libglib2.0-0 \
        fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

# ── Python 패키지 ────────────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── 소스 복사 ────────────────────────────────────────────────────────────────
COPY regulation_parser.py .

# ── 입출력 디렉터리 생성 ──────────────────────────────────────────────────────
RUN mkdir -p data/input_files data/output_mds

# ── 실행 ─────────────────────────────────────────────────────────────────────
CMD ["python", "regulation_parser.py"]
