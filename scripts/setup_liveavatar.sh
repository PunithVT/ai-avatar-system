#!/usr/bin/env bash
# Install Alibaba LiveAvatar as an optional avatar engine.
# Run from the project root:  bash scripts/setup_liveavatar.sh
#
# HARDWARE: 48 GB VRAM with FP8, 80 GB without. This will not run on the
# 16-24 GB cards MuseTalk targets. MuseTalk stays the default; this is an
# additional option, not a replacement.
#
# SPEED: LiveAvatar's inference script runs once and exits, so every generation
# loads the 14B model from scratch — minutes per turn. It suits the offline
# Celery render path, not the live WebSocket one.
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
MODELS_DIR="$BACKEND_DIR/models"
LIVE_DIR="$MODELS_DIR/LiveAvatar"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python"
VENV_PIP="$BACKEND_DIR/venv/bin/pip"

[ -f "$VENV_PYTHON" ] || VENV_PYTHON="python3"
[ -f "$VENV_PIP" ] || VENV_PIP="pip3"

echo "=== LiveAvatar Setup ==="
echo "Target : $LIVE_DIR"
echo ""

# Refuse early rather than after a ~60 GB download. Checking here costs a
# second; discovering it after the weights land costs the whole download.
echo "[1/4] Checking hardware..."
"$VENV_PYTHON" - <<'PYEOF'
import sys
try:
    import torch
except ImportError:
    sys.exit("ERROR: torch is not installed. Set up the backend venv first.")
if not torch.cuda.is_available():
    sys.exit("ERROR: no CUDA GPU detected. LiveAvatar needs 48 GB+ of VRAM.")
vram = torch.cuda.get_device_properties(0).total_memory / 1024**3
name = torch.cuda.get_device_name(0)
print(f"      {name}: {vram:.0f} GB VRAM")
if vram < 44:
    sys.exit(
        f"ERROR: {vram:.0f} GB is not enough. LiveAvatar needs ~48 GB with FP8 "
        f"(80 GB without). Stay on AVATAR_ENGINE=musetalk."
    )
if vram < 76:
    print("      NOTE: under 80 GB — FP8 is required. Keep LIVEAVATAR_FP8=true.")
PYEOF

echo "[2/4] Cloning LiveAvatar..."
if [ -d "$LIVE_DIR/.git" ]; then
  echo "      Already present, pulling..."
  git -C "$LIVE_DIR" pull --ff-only
else
  mkdir -p "$MODELS_DIR"
  git clone https://github.com/Alibaba-Quark/LiveAvatar.git "$LIVE_DIR"
fi

echo "[3/4] Installing Python dependencies..."
if [ -f "$LIVE_DIR/requirements.txt" ]; then
  # Into the backend venv deliberately: the animator launches torchrun from
  # this interpreter. Upstream may pin torch differently from our 2.2.0 — if
  # pip reports a conflict here, give LiveAvatar its own venv and point
  # LIVEAVATAR_PATH at a wrapper, rather than letting it move our pins and
  # break MuseTalk.
  "$VENV_PIP" install -r "$LIVE_DIR/requirements.txt" || {
    echo ""
    echo "WARN: dependency install failed. LiveAvatar's pins likely conflict"
    echo "      with the backend's (torch 2.2.0 for MuseTalk). Do NOT force it —"
    echo "      that breaks the default engine. Use a separate venv instead."
    exit 1
  }
else
  echo "      No requirements.txt upstream; skipping."
fi

echo "[4/4] Downloading weights (~60 GB — this takes a while)..."
mkdir -p "$LIVE_DIR/ckpt"
"$VENV_PYTHON" -m pip install -q "huggingface_hub[cli]"
"$VENV_PYTHON" -m huggingface_hub.commands.huggingface_cli download \
  Wan-AI/Wan2.2-S2V-14B --local-dir "$LIVE_DIR/ckpt/Wan2.2-S2V-14B"
"$VENV_PYTHON" -m huggingface_hub.commands.huggingface_cli download \
  Quark-Vision/Live-Avatar --local-dir "$LIVE_DIR/ckpt/LiveAvatar"

cat <<'EOF'

=== Done ===

Enable it in .env:

    AVATAR_ENGINE=liveavatar

MuseTalk stays installed and is one setting away:

    AVATAR_ENGINE=musetalk

Expect minutes per generation — the 14B model loads on every invocation, so
this belongs on the offline render path, not live conversation. Any failure
falls back to the simple ffmpeg engine rather than failing the turn.
EOF
