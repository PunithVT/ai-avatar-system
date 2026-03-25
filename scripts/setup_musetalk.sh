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
  "diffusers==0.32.2" \
  "accelerate==0.28.0" \
  "transformers==4.39.2" \
  "opencv-python==4.9.0.80" \
  "soundfile==0.12.1" \
  "librosa==0.11.0" \
  "einops==0.8.1" \
  "omegaconf" \
  "pyyaml" \
  "imageio" \
  "imageio[ffmpeg]" \
  "ffmpeg-python" \
  "moviepy<2" \
  "mediapipe" \
  "face-alignment" \
  "safetensors" \
  "timm"

# ── 3. Replace preprocessing.py with CPU-compatible version ─────────────────
echo ""
echo "[3/5] Writing CPU-compatible preprocessing.py (replaces mmpose with face_alignment)..."

PREPROCESS="$MUSETALK_DIR/musetalk/utils/preprocessing.py"
# Always overwrite — idempotent, works on first run and re-runs
cp "$PREPROCESS" "${PREPROCESS}.orig" 2>/dev/null || true
cat > "$PREPROCESS" << 'PYEOF'
# preprocessing.py — rewritten for Python 3.12 / CPU-only compatibility
# Uses face_alignment (pip install face-alignment) instead of mmpose/mmcv.
import os
import json
import pickle
import numpy as np
import cv2
import torch
from tqdm import tqdm
import face_alignment

# Initialise face alignment on CPU or GPU automatically
_device = "cuda" if torch.cuda.is_available() else "cpu"
_fa = face_alignment.FaceAlignment(
    face_alignment.LandmarksType.TWO_D,
    flip_input=False,
    device=_device,
)

# Sentinel value used by callers when no face is detected in a frame
coord_placeholder = (0.0, 0.0, 0.0, 0.0)


def resize_landmark(landmark, w, h, new_w, new_h):
    landmark_norm = landmark / np.array([w, h])
    return landmark_norm * np.array([new_w, new_h])


def read_imgs(img_list):
    frames = []
    print("reading images...")
    for img_path in tqdm(img_list):
        frame = cv2.imread(img_path)
        frames.append(frame)
    return frames


def _detect_face_bbox(frame_bgr, bbox_shift=0):
    """Detect face bounding box using 68-point landmarks. Returns (x1,y1,x2,y2) or None."""
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    preds = _fa.get_landmarks(frame_rgb)
    if preds is None or len(preds) == 0:
        return None
    lm = preds[0]  # (68, 2)
    x1, y1 = int(lm[:, 0].min()), int(lm[:, 1].min())
    x2, y2 = int(lm[:, 0].max()), int(lm[:, 1].max())
    y1 = max(0, y1 + bbox_shift)
    if x2 <= x1 or y2 <= y1 or x1 < 0:
        return None
    return (x1, y1, x2, y2)


def get_landmark_and_bbox(img_list, upperbondrange=0):
    """
    Detect face bounding boxes for all images.
    Returns (coords_list, frames).
    """
    frames = read_imgs(img_list)
    coords_list = []
    print(f"Getting face bounding boxes (bbox_shift={upperbondrange})..." if upperbondrange != 0
          else "Getting face bounding boxes...")
    for frame in tqdm(frames):
        bbox = _detect_face_bbox(frame, bbox_shift=upperbondrange)
        coords_list.append(bbox if bbox is not None else coord_placeholder)
    return coords_list, frames


def get_bbox_range(img_list, upperbondrange=0):
    frames = read_imgs(img_list)
    deltas = []
    for frame in tqdm(frames):
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        preds = _fa.get_landmarks(frame_rgb)
        if preds is None or len(preds) == 0:
            continue
        lm = preds[0]
        deltas.append(int(lm[:, 1].max()) - int(lm[:, 1].mean()))
    if not deltas:
        return "No faces detected"
    avg = int(sum(deltas) / len(deltas))
    return f"Total frame: {len(frames)}  Adjust range: [-{avg}~{avg}]  current value: {upperbondrange}"
PYEOF
echo "  preprocessing.py replaced ✓"

# ── 4. Download model weights from HuggingFace ──────────────────────────────
echo ""
echo "[4/5] Downloading MuseTalk model weights (~8 GB total)..."

"$VENV_PYTHON" - "$MUSETALK_DIR" << 'PYEOF'
from huggingface_hub import snapshot_download
import os, sys

musetalk_dir = sys.argv[1]
models_target = os.path.join(musetalk_dir, "models")
os.makedirs(models_target, exist_ok=True)

# ── MuseTalk weights (unet + VAE) ────────────────────────────────────────────
print("  Downloading TMElyralab/MuseTalk weights (~7 GB)...")
snapshot_download(
    repo_id="TMElyralab/MuseTalk",
    local_dir=models_target,
    ignore_patterns=["*.md", "*.txt", "*.gitattributes"],
)
print("  MuseTalk weights done.")

# ── Whisper-tiny (audio feature extractor, ~150 MB) ─────────────────────────
whisper_target = os.path.join(models_target, "whisper")
if not os.path.isdir(whisper_target) or not os.listdir(whisper_target):
    print("  Downloading openai/whisper-tiny (~150 MB)...")
    snapshot_download(
        repo_id="openai/whisper-tiny",
        local_dir=whisper_target,
        ignore_patterns=["*.md", "*.gitattributes", "flax_model*", "tf_model*", "rust_model*"],
    )
    print("  Whisper-tiny done.")
else:
    print("  Whisper-tiny already present — skipping.")

print("  All model downloads complete.")
PYEOF

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
