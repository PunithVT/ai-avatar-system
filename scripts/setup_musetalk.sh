#!/usr/bin/env bash
# Setup MuseTalk V1.5 for lip-sync animation (replaces SadTalker)
# Run from the project root:  bash scripts/setup_musetalk.sh
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
MODELS_DIR="$BACKEND_DIR/models"
MUSETALK_DIR="$MODELS_DIR/MuseTalk"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python"
SENTINEL="$PROJECT_ROOT/.musetalk_ready"

if [ ! -f "$VENV_PYTHON" ]; then
  VENV_PYTHON="python3"
fi

echo "=== MuseTalk V1.5 Setup ==="
echo "Project : $PROJECT_ROOT"
echo "Backend : $BACKEND_DIR"
echo "Python  : $VENV_PYTHON"
echo ""

# ── 1. Clone MuseTalk ────────────────────────────────────────────────────────
if [ -d "$MUSETALK_DIR/.git" ]; then
  echo "[1/5] MuseTalk already cloned — pulling latest..."
  git -C "$MUSETALK_DIR" pull --ff-only
else
  echo "[1/5] Cloning MuseTalk..."
  mkdir -p "$MODELS_DIR"
  git clone https://github.com/TMElyralab/MuseTalk.git "$MUSETALK_DIR"
fi

# ── 2. Install Python dependencies ──────────────────────────────────────────
echo ""
echo "[2/5] Installing MuseTalk requirements..."

# Install requirements, skipping packages incompatible with Python 3.12
# (mmpose/mmcv have no 3.12 wheels; tensorflow is optional for our use case)
"$VENV_PYTHON" -m pip install -q \
  diffusers==0.30.2 \
  accelerate==0.28.0 \
  transformers==4.39.2 \
  opencv-python==4.9.0.80 \
  soundfile==0.12.1 \
  librosa==0.11.0 \
  einops==0.8.1 \
  omegaconf \
  pyyaml \
  huggingface_hub \
  imageio \
  "imageio[ffmpeg]" \
  ffmpeg-python \
  moviepy \
  mediapipe \
  face-alignment \
  safetensors \
  timm

# ── 3. Patch preprocessing for Python 3.12 (replace mmpose with mediapipe) ─
echo ""
echo "[3/5] Patching MuseTalk preprocessing for Python 3.12..."

PREPROCESS="$MUSETALK_DIR/musetalk/utils/preprocessing.py"
if grep -q "mmpose\|mmdet\|mmcv" "$PREPROCESS" 2>/dev/null; then
  # Backup original
  cp "$PREPROCESS" "${PREPROCESS}.orig"

  # Replace mmpose/mmdet imports with mediapipe equivalents
  "$VENV_PYTHON" - << 'PYEOF'
import re, sys
from pathlib import Path

src = Path(sys.argv[1])
code = src.read_text()

# Replace the mmpose/mmdet import block
old_block = re.search(
    r'from mmpose.*?(?=\n\S|\nclass|\ndef|\Z)',
    code, re.DOTALL
)
if old_block:
    replacement = (
        "# mmpose replaced with mediapipe for Python 3.12 compatibility\n"
        "import mediapipe as mp\n"
        "import face_alignment\n"
        "_fa = face_alignment.FaceAlignment(\n"
        "    face_alignment.LandmarksType.TWO_D, flip_input=False\n"
        ")\n"
    )
    code = code[:old_block.start()] + replacement + code[old_block.end():]
    src.write_text(code)
    print(f"  Patched {src}")
else:
    print("  No mmpose imports found — skipping patch")
PYEOF "$PREPROCESS"
  echo "  Preprocessing patch applied ✓"
else
  echo "  No mmpose imports in preprocessing — no patch needed ✓"
fi

# ── 4. Download model weights from HuggingFace ──────────────────────────────
echo ""
echo "[4/5] Downloading MuseTalk model weights (~3 GB)..."

"$VENV_PYTHON" - << 'PYEOF'
from huggingface_hub import snapshot_download
import os, sys

musetalk_dir = sys.argv[1]
models_target = os.path.join(musetalk_dir, "models")
os.makedirs(models_target, exist_ok=True)

print("  Downloading TMElyralab/MuseTalk weights...")
snapshot_download(
    repo_id="TMElyralab/MuseTalk",
    local_dir=models_target,
    local_dir_use_symlinks=False,
    ignore_patterns=["*.md", "*.txt", "*.gitattributes"],
)
print("  Model download complete.")
PYEOF "$MUSETALK_DIR"

# ── 5. Verify and write sentinel ─────────────────────────────────────────────
echo ""
echo "[5/5] Verifying installation..."

MISSING=0

if [ ! -f "$MUSETALK_DIR/scripts/inference.py" ]; then
  echo "  MISSING: scripts/inference.py"
  MISSING=1
else
  echo "  scripts/inference.py ✓"
fi

if [ ! -f "$MUSETALK_DIR/models/musetalkV15/unet.pth" ]; then
  echo "  MISSING: models/musetalkV15/unet.pth"
  MISSING=1
else
  echo "  models/musetalkV15/unet.pth ✓"
fi

if [ ! -d "$MUSETALK_DIR/models/whisper" ]; then
  echo "  MISSING: models/whisper/"
  MISSING=1
else
  echo "  models/whisper/ ✓"
fi

if [ "$MISSING" -eq 0 ]; then
  touch "$SENTINEL"
  echo ""
  echo "  Sentinel written: $SENTINEL"
  echo ""
  echo "=== MuseTalk setup complete! ==="
  echo "Set AVATAR_ENGINE=musetalk in .env and restart the backend."
else
  echo ""
  echo "Some checks failed — see above. Fix and re-run."
  exit 1
fi
