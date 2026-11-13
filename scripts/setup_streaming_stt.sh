#!/usr/bin/env bash
# Download the streaming speech-to-text model (NVIDIA Nemotron 3.5 ASR
# Streaming 0.6B, int8, exported for sherpa-onnx).
# Run from the project root:  bash scripts/setup_streaming_stt.sh
#
# Optional. Without it, STT_STREAMING=sherpa logs a warning once and every
# utterance is transcribed by Whisper, as it is with STT_STREAMING=off.
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
STT_DIR="$BACKEND_DIR/models/stt"
MODEL_NAME="sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-560ms-int8-2026-06-11"
MODEL_DIR="$STT_DIR/$MODEL_NAME"
ARCHIVE="$STT_DIR/$MODEL_NAME.tar.bz2"
URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$MODEL_NAME.tar.bz2"
MODEL_FILES="encoder.int8.onnx decoder.int8.onnx joiner.int8.onnx tokens.txt"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python"

[ -f "$VENV_PYTHON" ] || VENV_PYTHON="python3"

have_model() {
  for f in $MODEL_FILES; do
    # -s: present AND not empty. An interrupted unpack leaves empty files.
    [ -s "$MODEL_DIR/$f" ] || return 1
  done
}

echo "=== Streaming Speech-to-Text (sherpa-onnx + Nemotron) Setup ==="

echo "[1/3] Downloading the model (~475 MB)..."
mkdir -p "$STT_DIR"
if have_model; then
  echo "      Already present, skipping."
else
  # Retried: a dropped connection halfway through a 475 MB release asset
  # otherwise leaves a truncated archive that fails later, at unpacking,
  # with a confusing error.
  for attempt in 1 2 3; do
    if curl -fSL --retry 3 --retry-delay 5 -o "$ARCHIVE.part" "$URL"; then
      mv "$ARCHIVE.part" "$ARCHIVE"
      break
    fi
    echo "      attempt $attempt failed; retrying..." >&2
    rm -f "$ARCHIVE.part"
    [ "$attempt" -lt 3 ] || { echo "ERROR: download failed after 3 attempts" >&2; exit 1; }
    sleep $((attempt * 5))
  done

  echo "[2/3] Unpacking..."
  # Into a scratch directory first, then moved into place: a half-unpacked
  # model directory would otherwise look installed to the next run.
  TMP_DIR="$STT_DIR/.unpack-$$"
  rm -rf "$TMP_DIR"
  mkdir -p "$TMP_DIR"
  if ! tar -xjf "$ARCHIVE" -C "$TMP_DIR"; then
    rm -rf "$TMP_DIR" "$ARCHIVE"
    echo "ERROR: could not unpack the archive (truncated download?). Re-run this script." >&2
    exit 1
  fi
  if [ ! -d "$TMP_DIR/$MODEL_NAME" ]; then
    rm -rf "$TMP_DIR"
    echo "ERROR: the archive did not contain $MODEL_NAME/." >&2
    exit 1
  fi
  rm -rf "$MODEL_DIR"
  mv "$TMP_DIR/$MODEL_NAME" "$MODEL_DIR"
  rm -rf "$TMP_DIR" "$ARCHIVE"
fi

echo "[3/3] Verifying..."
for f in $MODEL_FILES; do
  if [ ! -s "$MODEL_DIR/$f" ]; then
    echo "ERROR: $MODEL_DIR/$f is missing or empty. Delete $MODEL_DIR and re-run." >&2
    exit 1
  fi
done
echo "      OK - model files present in backend/models/stt/$MODEL_NAME/"
# The runtime is in requirements.txt; say so if this environment predates it.
if "$VENV_PYTHON" -c "import importlib.util, sys; sys.exit(importlib.util.find_spec('sherpa_onnx') is None)" 2>/dev/null; then
  echo "      OK - sherpa-onnx is installed."
else
  echo "      WARN: the sherpa-onnx package is not installed in this environment."
  echo "            Run: pip install -r backend/requirements.txt"
fi

cat <<'EOF'

=== Done ===

Enable it in .env:

    STT_STREAMING=sherpa

Then restart the backend. It applies to hands-free speech on the WebRTC
transport (MEDIA_TRANSPORT=webrtc with VOICE_TURN_DETECTION on): the user's
words appear while they speak, and the reply starts sooner. It holds a
~630 MB model in RAM and uses CPU while someone is speaking. Sessions in a
language the model is not accurate in keep using Whisper.
EOF
