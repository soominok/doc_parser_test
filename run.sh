#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# run.sh  —  Launch the Document → Markdown Converter GUI inside Docker
#
# Requirements (Linux host):
#   · Docker + Docker Compose v2
#   · An X11 display server (standard on desktop Linux)
#
# macOS:
#   Install XQuartz (https://www.xquartz.org/), then run:
#     defaults write org.xquartz.X11 nolisten_tcp 0
#     xhost + 127.0.0.1
#   Set DISPLAY=host.docker.internal:0 before running this script.
#
# Windows (WSL2):
#   Use WSLg (built-in to Windows 11) or install VcXsrv / X410.
#   Set DISPLAY=:0 or as needed by your X server.
#
# Usage:
#   ./run.sh                    # GUI mode
#   ./run.sh file.pdf           # CLI mode (output saved next to the file)
#   ./run.sh file.pdf ./output  # CLI mode with explicit output directory
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

IMAGE="doc-to-md:latest"
CONTAINER="doc_converter"

# ── Build image if not already built ─────────────────────────────────────────
if ! docker image inspect "$IMAGE" &>/dev/null; then
    echo ">>> Building Docker image (first run — this may take several minutes)…"
    docker build -t "$IMAGE" .
fi

# ── Shared docker run flags ───────────────────────────────────────────────────
DOCKER_FLAGS=(
    --rm
    --name "$CONTAINER"
    -e "DISPLAY=${DISPLAY:-:0}"
    -v "/tmp/.X11-unix:/tmp/.X11-unix:rw"
    -v "$(pwd)/output:/data/output"
    --network host
)

# ── CLI mode ──────────────────────────────────────────────────────────────────
if [[ $# -ge 1 ]]; then
    SRC="$1"
    OUT="${2:-$(dirname "$SRC")}"

    ABS_SRC="$(realpath "$SRC")"
    ABS_OUT="$(realpath "$OUT")"
    mkdir -p "$ABS_OUT"

    echo ">>> CLI mode: converting $ABS_SRC → $ABS_OUT"
    docker run "${DOCKER_FLAGS[@]}" \
        -v "${ABS_SRC}:${ABS_SRC}:ro" \
        -v "${ABS_OUT}:${ABS_OUT}" \
        "$IMAGE" \
        "$ABS_SRC" "$ABS_OUT"
    exit 0
fi

# ── GUI mode ──────────────────────────────────────────────────────────────────

# Allow the Docker container to connect to the local X server
xhost +local:docker 2>/dev/null || true

echo ">>> Launching GUI…"
echo "    Output will be saved to: $(pwd)/output"

docker run "${DOCKER_FLAGS[@]}" \
    -v "$(pwd):/data/workspace" \
    "$IMAGE"

# Revoke X11 access when done
xhost -local:docker 2>/dev/null || true
