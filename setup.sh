#!/usr/bin/env bash
# T3 voice bridge setup: venv + pip deps + Kokoro model files. Idempotent.
set -euo pipefail
cd "$(dirname "$0")"

VENV="${VENV:-bridge-venv}"
MODELS_DIR="${MODELS_DIR:-models}"
PYTHON="${PYTHON:-python3}"

echo "==> disk check"
df -h /home/ubuntu | tail -1

if [ ! -d "$VENV" ]; then
  echo "==> creating venv $VENV"
  "$PYTHON" -m venv "$VENV"
else
  echo "==> venv exists, reusing"
fi

# shellcheck disable=SC1091
. "$VENV/bin/activate"
pip install --upgrade pip
pip install kokoro-onnx soundfile numpy requests

mkdir -p "$MODELS_DIR"
BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"

# expected sizes (bytes, approximate lower bounds for verification)
need() { # need <file> <url> <min_bytes>
  local f="$1" url="$2" min="$3"
  if [ -f "$f" ]; then
    local sz
    sz=$(stat -c%s "$f")
    if [ "$sz" -ge "$min" ]; then
      echo "==> exists ok: $f ($sz bytes)"
      return 0
    fi
    echo "==> re-downloading (too small: $sz < $min): $f"
  else
    echo "==> downloading $f"
  fi
  curl -fL --retry 3 -o "$f" "$url"
  local sz2
  sz2=$(stat -c%s "$f")
  if [ "$sz2" -lt "$min" ]; then
    echo "ERROR: downloaded $f too small ($sz2 bytes), expected >= $min" >&2
    exit 1
  fi
  echo "==> ok: $f ($sz2 bytes)"
}

need "$MODELS_DIR/kokoro-v1.0.onnx" "$BASE/kokoro-v1.0.onnx" 250000000
need "$MODELS_DIR/voices-v1.0.bin"  "$BASE/voices-v1.0.bin"  10000000

echo "==> checking espeak-ng + ffmpeg"
command -v espeak-ng >/dev/null || { echo "ERROR: espeak-ng missing" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ERROR: ffmpeg missing" >&2; exit 1; }
espeak-ng --version | head -1
ffmpeg -version 2>/dev/null | head -1
echo "==> setup complete"
