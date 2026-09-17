#!/usr/bin/env bash
# Download GFPGAN weights for optional face restoration.
# Run from the project root:  bash scripts/setup_face_restore.sh
#
# Optional. Without it, FACE_RESTORE=gfpgan logs a warning once and the worker
# falls back to plain MuseTalk output — lip-sync keeps working either way.
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
MUSETALK_DIR="$BACKEND_DIR/models/MuseTalk"
GFPGAN_DIR="$MUSETALK_DIR/models/gfpgan"
VENV_PYTHON="$BACKEND_DIR/venv/bin/python"
VENV_PIP="$BACKEND_DIR/venv/bin/pip"

[ -f "$VENV_PYTHON" ] || VENV_PYTHON="python3"
[ -f "$VENV_PIP" ] || VENV_PIP="pip3"

echo "=== Face Restoration (GFPGAN) Setup ==="

# MuseTalk must be installed first: the weights live under its tree because the
# worker runs with MuseTalk as its cwd and resolves the path relative to it.
if [ ! -d "$MUSETALK_DIR" ]; then
  echo "ERROR: $MUSETALK_DIR not found. Run scripts/setup_musetalk.sh first." >&2
  exit 1
fi

echo "[1/4] Installing gfpgan..."
# From the requirements file, not a bare pin, so there is one source of
# truth for the version. Deliberately NOT in requirements.txt: basicsr
# builds from source and its setup.py collides with the CUDA image's
# cuda-toolkit, which would fail the backend image build for everyone.
"$VENV_PIP" install -q -r "$BACKEND_DIR/requirements-face-restore.txt"

echo "[2/4] Downloading GFPGANv1.4 weights (~333 MB)..."
mkdir -p "$GFPGAN_DIR"
WEIGHTS="$GFPGAN_DIR/GFPGANv1.4.pth"
if [ -f "$WEIGHTS" ]; then
  echo "      Already present, skipping."
else
  # Retried: this is a ~333 MB fetch from a release asset and a dropped
  # connection halfway through otherwise leaves a truncated file that fails
  # later, at inference, with a confusing error.
  for attempt in 1 2 3; do
    if curl -fSL --retry 3 --retry-delay 5 \
      -o "$WEIGHTS.part" \
      "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth"; then
      mv "$WEIGHTS.part" "$WEIGHTS"
      break
    fi
    echo "      attempt $attempt failed; retrying..." >&2
    rm -f "$WEIGHTS.part"
    [ "$attempt" -lt 3 ] || { echo "ERROR: download failed after 3 attempts" >&2; exit 1; }
    sleep $((attempt * 5))
  done
fi

# GFPGANer pulls two facexlib helper nets (detection + parsing, ~186 MB) on
# FIRST CONSTRUCTION, into ./gfpgan/weights relative to the process cwd. Left
# to runtime that download happens inside the worker's model-load timeout on a
# cold start, and fails outright on an air-gapped host. Fetch them now, into
# the MuseTalk directory the worker actually runs from.
echo "[3/4] Pre-fetching facexlib helper weights (~186 MB)..."
( cd "$MUSETALK_DIR" && "$VENV_PYTHON" - <<'PYEOF'
try:
    from facexlib.detection import init_detection_model
    from facexlib.parsing import init_parsing_model
    init_detection_model("retinaface_resnet50", half=False)
    init_parsing_model(model_name="parsenet")
    print("      OK - helper weights cached under gfpgan/weights/")
except Exception as e:
    # Not fatal: the worker fetches them itself on first run if it has network.
    print(f"      WARN: could not pre-fetch ({type(e).__name__}: {e}).")
    print("      The worker will download them on first use, which needs network.")
PYEOF
)

echo "[4/4] Verifying..."
"$VENV_PYTHON" - "$WEIGHTS" <<'PYEOF'
import sys, os
path = sys.argv[1]
size = os.path.getsize(path)
# The real file is ~333 MB; anything much smaller is a truncated download or an
# HTML error page saved under a .pth name.
if size < 100 * 1024 * 1024:
    sys.exit(f"ERROR: {path} is only {size/1e6:.1f} MB — looks truncated. Delete it and re-run.")
try:
    from gfpgan import GFPGANer  # noqa: F401
except Exception as e:
    sys.exit(f"ERROR: gfpgan did not import: {type(e).__name__}: {e}")
print(f"      OK — {size/1e6:.0f} MB, gfpgan imports cleanly.")
PYEOF

cat <<'EOF'

=== Done ===

Enable it in .env:

    FACE_RESTORE=gfpgan

Then restart the backend. Restoration runs per frame and costs real time, so
measure the FPS trade on your GPU before leaving it on — A/B it against
FACE_RESTORE=off on the same avatar and audio.
EOF
