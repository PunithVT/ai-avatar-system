#!/usr/bin/env bash
# Setup SadTalker for lip-sync animation
# Run from the project root:  bash scripts/setup_sadtalker.sh
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
MODELS_DIR="$BACKEND_DIR/models"
SADTALKER_DIR="$MODELS_DIR/SadTalker"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python"

# Fall back to system python if venv not found
if [ ! -f "$VENV_PYTHON" ]; then
  VENV_PYTHON="python3"
fi

echo "=== SadTalker Setup ==="
echo "Backend : $BACKEND_DIR"
echo "Python  : $VENV_PYTHON"
echo ""

# ── 1. Clone SadTalker ───────────────────────────────────────────────────────
if [ -d "$SADTALKER_DIR/.git" ]; then
  echo "[1/4] SadTalker already cloned — pulling latest..."
  git -C "$SADTALKER_DIR" pull --ff-only
else
  echo "[1/4] Cloning SadTalker..."
  mkdir -p "$MODELS_DIR"
  git clone https://github.com/OpenTalker/SadTalker.git "$SADTALKER_DIR"
fi

# ── 2. Install SadTalker Python dependencies into the venv ──────────────────
echo ""
echo "[2/4] Installing SadTalker requirements..."
"$VENV_PYTHON" -m pip install -q \
  face_alignment==1.3.5 \
  imageio==2.19.3 imageio-ffmpeg==0.4.7 \
  kornia==0.6.8 \
  resampy==0.3.1 \
  yacs==0.1.8 \
  basicsr==1.4.2 \
  facexlib==0.3.0 \
  gfpgan \
  safetensors \
  av

# ── 3. Download SadTalker checkpoints ───────────────────────────────────────
CKPT_DIR="$SADTALKER_DIR/checkpoints"
GFPGAN_DIR="$SADTALKER_DIR/gfpgan/weights"
mkdir -p "$CKPT_DIR" "$GFPGAN_DIR"

BASE_URL="https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc"
FACEXLIB_URL="https://github.com/xinntao/facexlib/releases/download/v0.1.0"
FACEXLIB_V2_URL="https://github.com/xinntao/facexlib/releases/download/v0.2.2"
GFPGAN_URL="https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0"

download_if_missing() {
  local url="$1"
  local dest="$2"
  if [ -f "$dest" ]; then
    echo "  (already exists) $(basename "$dest")"
  else
    echo "  Downloading $(basename "$dest")..."
    curl -L --progress-bar -o "$dest" "$url"
  fi
}

echo ""
echo "[3/4] Downloading SadTalker model checkpoints..."
download_if_missing "$BASE_URL/mapping_00109-model.pth.tar"         "$CKPT_DIR/mapping_00109-model.pth.tar"
download_if_missing "$BASE_URL/mapping_00229-model.pth.tar"         "$CKPT_DIR/mapping_00229-model.pth.tar"
download_if_missing "$BASE_URL/SadTalker_V0.0.2_256.safetensors"   "$CKPT_DIR/SadTalker_V0.0.2_256.safetensors"
download_if_missing "$BASE_URL/SadTalker_V0.0.2_512.safetensors"   "$CKPT_DIR/SadTalker_V0.0.2_512.safetensors"

echo ""
echo "  Downloading GFPGAN / facexlib weights..."
download_if_missing "$FACEXLIB_URL/alignment_WFLW_4HG.pth"         "$GFPGAN_DIR/alignment_WFLW_4HG.pth"
download_if_missing "$FACEXLIB_URL/detection_Resnet50_Final.pth"    "$GFPGAN_DIR/detection_Resnet50_Final.pth"
download_if_missing "$GFPGAN_URL/GFPGANv1.4.pth"                   "$GFPGAN_DIR/GFPGANv1.4.pth"
download_if_missing "$FACEXLIB_V2_URL/parsing_parsenet.pth"         "$GFPGAN_DIR/parsing_parsenet.pth"

# ── 4. Quick smoke-test ──────────────────────────────────────────────────────
echo ""
echo "[4/4] Verifying installation..."
"$VENV_PYTHON" - <<'EOF'
import sys, pathlib
sadtalker = pathlib.Path(__file__).parent / "../../backend/models/SadTalker"
EOF

if [ -f "$SADTALKER_DIR/inference.py" ]; then
  echo "  inference.py found ✓"
else
  echo "  ERROR: inference.py not found in $SADTALKER_DIR"
  exit 1
fi

MISSING=0
for f in \
  "$CKPT_DIR/mapping_00109-model.pth.tar" \
  "$CKPT_DIR/SadTalker_V0.0.2_256.safetensors" \
  "$GFPGAN_DIR/GFPGANv1.4.pth"; do
  if [ ! -f "$f" ]; then
    echo "  MISSING: $f"
    MISSING=1
  fi
done

if [ "$MISSING" -eq 0 ]; then
  # Write sentinel so start.sh knows setup is done (avoids re-scanning model files)
  touch "$PROJECT_ROOT/.sadtalker_ready"
  echo ""
  echo "=== SadTalker setup complete! ==="
  echo "Restart the backend to pick up the change."
else
  echo ""
  echo "Some files are missing — check your internet connection and re-run."
  exit 1
fi
