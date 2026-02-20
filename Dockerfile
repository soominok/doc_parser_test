# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── Environment ───────────────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    # Docling model cache inside the container
    DOCLING_CACHE_DIR=/tmp/docling_cache \
    # Default X11 display (override at runtime with -e DISPLAY=:0)
    DISPLAY=:0

# ── System dependencies ───────────────────────────────────────────────────────
#
# python3-tk / tk-dev  → tkinter GUI
# libx11-* libxft* …   → X11 display forwarding
# fonts-noto-cjk       → Korean/Chinese/Japanese character rendering
# poppler-utils        → PDF utilities (used internally by pdfplumber)
# libgl1               → OpenCV / pymupdf runtime dep
#
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-tk \
        tk-dev \
        libx11-6 \
        libxft2 \
        libxss1 \
        libxext6 \
        libxrender1 \
        libglib2.0-0 \
        libfontconfig1 \
        libfreetype6 \
        libgl1 \
        poppler-utils \
        fonts-noto-cjk \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Application ───────────────────────────────────────────────────────────────
WORKDIR /app

COPY requirements.txt ./

# Install Python dependencies
# docling pulls in torch + transformers; build can be slow on first run.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY converter.py ./

# ── Volume for user documents ─────────────────────────────────────────────────
# Mount your documents here: -v /host/path:/data
VOLUME ["/data"]

# Default working directory when running the container
WORKDIR /data

# ── Entry point ───────────────────────────────────────────────────────────────
#
# GUI mode  : docker run -e DISPLAY=... -v /tmp/.X11-unix:/tmp/.X11-unix … image
# CLI mode  : docker run … image /data/file.pdf /data/output
#
ENTRYPOINT ["python", "/app/converter.py"]
