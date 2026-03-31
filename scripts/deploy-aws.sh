#!/bin/bash
# ============================================================
# deploy-aws.sh — Bootstrap and deploy on an AWS GPU EC2 instance
#
# Tested on: Ubuntu 22.04 LTS (g5.xlarge / g6.xlarge)
# Recommended AMI: "Deep Learning OSS Nvidia Driver AMI GPU PyTorch 2.2 (Ubuntu 22.04)"
#   → CUDA + nvidia-docker2 are pre-installed, skipping installation steps
#
# Usage:
#   # On a fresh EC2 instance (plain Ubuntu 22.04):
#   bash scripts/deploy-aws.sh
#
#   # Update/redeploy on an already-configured instance:
#   bash scripts/deploy-aws.sh --update
# ============================================================
set -e

REPO_URL="https://github.com/PunithVT/ai-avatar-system.git"
APP_DIR="/opt/ai-avatar-system"
UPDATE_MODE=false

for arg in "$@"; do
  [ "$arg" = "--update" ] && UPDATE_MODE=true
done

echo "========================================"
echo " AI Avatar System — AWS GPU Deployment"
echo "========================================"

# ── 1. Install Docker if missing ─────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  echo "[1/6] Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker ubuntu || true
  systemctl enable docker
  systemctl start docker
else
  echo "[1/6] Docker already installed — skipping"
fi

# ── 2. Install nvidia-docker2 if missing ─────────────────────────────────────
if ! dpkg -l | grep -q nvidia-docker2; then
  echo "[2/6] Installing nvidia-docker2..."
  distribution=$(. /etc/os-release; echo $ID$VERSION_ID)
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L "https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list" | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq
  apt-get install -y nvidia-docker2
  systemctl restart docker
  echo "nvidia-docker2 installed"
else
  echo "[2/6] nvidia-docker2 already installed — skipping"
fi

# Verify GPU is visible to Docker
echo "GPU check:"
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi --query-gpu=name,memory.total --format=csv,noheader \
  || { echo "ERROR: GPU not accessible in Docker. Check your instance type has a GPU."; exit 1; }

# ── 3. Clone or update the repo ──────────────────────────────────────────────
if [ "$UPDATE_MODE" = true ] && [ -d "$APP_DIR" ]; then
  echo "[3/6] Updating repo..."
  cd "$APP_DIR"
  git pull origin main
else
  echo "[3/6] Cloning repo to $APP_DIR..."
  rm -rf "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi

# ── 4. Set up environment file ───────────────────────────────────────────────
echo "[4/6] Environment setup..."
if [ ! -f "$APP_DIR/.env.prod" ]; then
  cp "$APP_DIR/.env.prod.example" "$APP_DIR/.env.prod"
  echo ""
  echo "  !! ACTION REQUIRED: Edit $APP_DIR/.env.prod with your API keys and settings"
  echo "  Then re-run: bash scripts/deploy-aws.sh --update"
  echo ""
  exit 0
fi

# ── 5. Download MuseTalk models (first deploy only) ──────────────────────────
if [ ! -f "$APP_DIR/backend/models/MuseTalk/models/musetalkV15/unet.pth" ]; then
  echo "[5/6] Downloading MuseTalk models (~9 GB, takes 5-10 min)..."
  bash "$APP_DIR/scripts/setup_musetalk.sh"
else
  echo "[5/6] MuseTalk models already present — skipping"
fi

# ── 6. Build and start services ──────────────────────────────────────────────
echo "[6/6] Building and starting services..."
cd "$APP_DIR"
docker compose -f docker-compose.yml -f docker-compose.prod.yml build --no-cache
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo " Deployment complete!"
echo "========================================"
PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "unknown")
echo ""
echo "  Public IP   : $PUBLIC_IP"
echo "  Frontend    : http://$PUBLIC_IP"
echo "  API         : http://$PUBLIC_IP/api/v1"
echo "  API Docs    : http://$PUBLIC_IP/docs"
echo ""
echo "  GPU status  :"
docker exec avatar-backend nvidia-smi --query-gpu=name,memory.used,memory.total,temperature.gpu \
  --format=csv,noheader 2>/dev/null || echo "  (GPU info not yet available — backend still starting)"
echo ""
echo "  Logs        : docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f backend"
echo "========================================"
