#!/usr/bin/env bash
# Setup SadTalker for lip-sync animation
# Run from the project root:  bash scripts/setup_sadtalker.sh
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
MODELS_DIR="$BACKEND_DIR/models"
SADTALKER_DIR="$MODELS_DIR/SadTalker"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python"
SENTINEL="$PROJECT_ROOT/.sadtalker_ready"

# Fall back to system python if venv not found
if [ ! -f "$VENV_PYTHON" ]; then
  VENV_PYTHON="python3"
fi

echo "=== SadTalker Setup ==="
echo "Project : $PROJECT_ROOT"
echo "Backend : $BACKEND_DIR"
echo "Python  : $VENV_PYTHON"
echo ""

# ── 1. Clone SadTalker ───────────────────────────────────────────────────────
if [ -d "$SADTALKER_DIR/.git" ]; then
  echo "[1/6] SadTalker already cloned — pulling latest..."
  git -C "$SADTALKER_DIR" pull --ff-only
else
  echo "[1/6] Cloning SadTalker..."
  mkdir -p "$MODELS_DIR"
  git clone https://github.com/OpenTalker/SadTalker.git "$SADTALKER_DIR"
fi

# ── 2. Install SadTalker Python dependencies into the venv ──────────────────
echo ""
echo "[2/6] Installing SadTalker requirements..."
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

# ── 3. Patch SadTalker source for NumPy ≥ 1.24 ──────────────────────────────
# np.float / np.int / np.bool / np.complex were removed in NumPy 1.24.
echo ""
echo "[3/6] Patching SadTalker for NumPy >= 1.24..."
find "$SADTALKER_DIR/src" -name "*.py" -exec \
  sed -i \
    -e 's/\bnp\.float\b/np.float64/g' \
    -e 's/\bnp\.int\b/np.int64/g' \
    -e 's/\bnp\.complex\b/np.complex128/g' \
    -e 's/\bnp\.bool\b/np.bool_/g' \
    -e 's/\bnp\.object\b/object/g' \
    -e 's/\bnp\.str\b/np.str_/g' \
  {} +
echo "  NumPy alias patch applied ✓"

# ── 4. Patch basicsr for torchvision ≥ 0.16 ─────────────────────────────────
# basicsr 1.4.2 imports torchvision.transforms.functional_tensor which was
# removed in torchvision 0.16. Create a compatibility shim if needed.
echo ""
echo "[4/6] Checking torchvision compatibility..."
"$VENV_PYTHON" -c "from torchvision.transforms.functional_tensor import rgb_to_grayscale" 2>/dev/null \
  && echo "  torchvision.transforms.functional_tensor OK" \
  || {
    echo "  Applying functional_tensor compatibility shim..."
    "$VENV_PYTHON" - << 'PYEOF'
import os, torchvision.transforms as T
shim = os.path.join(os.path.dirname(T.__file__), "functional_tensor.py")
if not os.path.exists(shim):
    with open(shim, "w") as f:
        f.write(
            "# Compatibility shim for basicsr / torchvision >= 0.16\n"
            "from torchvision.transforms.functional import *\n"
            "from torchvision.transforms.functional import rgb_to_grayscale\n"
        )
    print(f"  Created shim at {shim}")
else:
    print("  Shim already exists")
PYEOF
  }

# ── 4. Download SadTalker checkpoints ───────────────────────────────────────
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
echo "[5/6] Downloading SadTalker model checkpoints..."
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

# ── 5. Verify and write sentinel ─────────────────────────────────────────────
echo ""
echo "[6/6] Verifying installation..."

MISSING=0

if [ ! -f "$SADTALKER_DIR/inference.py" ]; then
  echo "  MISSING: inference.py not found in $SADTALKER_DIR"
  MISSING=1
else
  echo "  inference.py ✓"
fi

for f in \
  "$CKPT_DIR/mapping_00109-model.pth.tar" \
  "$CKPT_DIR/SadTalker_V0.0.2_256.safetensors" \
  "$GFPGAN_DIR/GFPGANv1.4.pth"; do
  if [ ! -f "$f" ]; then
    echo "  MISSING: $f"
    MISSING=1
  else
    echo "  $(basename "$f") ✓"
  fi
done

# Verify the torchvision shim works end-to-end
if ! "$VENV_PYTHON" -c "from gfpgan import GFPGANer" 2>/dev/null; then
  echo "  WARNING: gfpgan import still failing — SadTalker may not work"
  MISSING=1
else
  echo "  gfpgan import ✓"
fi

if [ "$MISSING" -eq 0 ]; then
  touch "$SENTINEL"
  echo ""
  echo "  Sentinel written: $SENTINEL"
  echo ""
  echo "=== SadTalker setup complete! ==="
  echo "Restart the backend to pick up the change."
else
  echo ""
  echo "Some checks failed — see above. Fix and re-run."
  exit 1
fi
