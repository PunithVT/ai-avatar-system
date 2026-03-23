#!/usr/bin/env bash
# =============================================================================
#  AvatarAI — One-command startup script
#  Usage:
#    ./start.sh              # Docker mode (default)
#    ./start.sh --dev        # Manual/dev mode (no Docker)
#    ./start.sh --stop       # Stop all services
#    ./start.sh --logs       # Tail logs (Docker mode)
#    ./start.sh --status     # Show service status
# =============================================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
step()    { echo -e "\n${BOLD}▶ $*${RESET}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MODE="docker"
[[ "${1:-}" == "--dev"    ]] && MODE="dev"
[[ "${1:-}" == "--stop"   ]] && MODE="stop"
[[ "${1:-}" == "--logs"   ]] && MODE="logs"
[[ "${1:-}" == "--status" ]] && MODE="status"

# ── Banner ────────────────────────────────────────────────────────────────────
echo -e "${BOLD}${CYAN}"
echo "  ╔═══════════════════════════════════════╗"
echo "  ║   🎭  AvatarAI — Real-Time Avatars    ║"
echo "  ╚═══════════════════════════════════════╝"
echo -e "${RESET}"

# =============================================================================
#  STOP
# =============================================================================
if [[ "$MODE" == "stop" ]]; then
  step "Stopping all services"
  if command -v docker &>/dev/null; then
    docker compose down --remove-orphans && success "Docker services stopped"
  fi
  # Kill any manual dev processes
  pkill -f "uvicorn main:app" 2>/dev/null && success "Backend stopped" || true
  pkill -f "next dev"         2>/dev/null && success "Frontend stopped" || true
  pkill -f "celery -A app"    2>/dev/null && success "Celery stopped" || true
  exit 0
fi

# =============================================================================
#  LOGS
# =============================================================================
if [[ "$MODE" == "logs" ]]; then
  docker compose logs -f --tail=50
  exit 0
fi

# =============================================================================
#  STATUS
# =============================================================================
if [[ "$MODE" == "status" ]]; then
  step "Service Status"
  docker compose ps 2>/dev/null || echo "Docker not running"
  echo ""
  for url in \
    "Frontend      http://localhost:3000" \
    "Backend API   http://localhost:8000/health" \
    "Swagger Docs  http://localhost:8000/docs" \
    "Celery Flower http://localhost:5555" \
    "Prometheus    http://localhost:9090"
  do
    name=$(echo "$url" | awk '{print $1}')
    addr=$(echo "$url" | awk '{print $2}')
    if curl -sf "$addr" &>/dev/null; then
      echo -e "  ${GREEN}●${RESET} $name  →  $addr"
    else
      echo -e "  ${RED}●${RESET} $name  →  $addr  (not responding)"
    fi
  done
  exit 0
fi

# =============================================================================
#  PREFLIGHT CHECKS
# =============================================================================
step "Preflight checks"

# .env file
if [[ ! -f ".env" ]]; then
  warn ".env not found — creating from .env.example"
  cp .env.example .env
  echo ""
  echo -e "  ${YELLOW}ACTION REQUIRED:${RESET} Open ${BOLD}.env${RESET} and set at least one API key:"
  echo -e "  ${CYAN}ANTHROPIC_API_KEY${RESET}  or  ${CYAN}OPENAI_API_KEY${RESET}"
  echo ""
  read -rp "  Press Enter to continue anyway, or Ctrl+C to edit .env first..."
else
  success ".env found"
fi

# Check if any LLM key is set
source .env 2>/dev/null || true
if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" ]]; then
  warn "No LLM API key set — LLM responses will fail."
  warn "Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env, or use Ollama (LLM_PROVIDER=ollama)."
fi

# =============================================================================
#  DOCKER MODE  (default)
# =============================================================================
if [[ "$MODE" == "docker" ]]; then

  step "Checking Docker"
  if ! command -v docker &>/dev/null; then
    error "Docker not found. Install Docker Desktop: https://docs.docker.com/get-docker/"
    exit 1
  fi
  if ! docker info &>/dev/null; then
    error "Docker daemon is not running. Start Docker Desktop first."
    exit 1
  fi
  success "Docker is running"

  step "Building and starting all services"
  info "This may take a few minutes on first run (downloading images + building)..."
  echo ""

  docker compose up -d --build

  step "Waiting for services to be healthy"
  echo -n "  Postgres "
  for i in $(seq 1 30); do
    if docker exec avatar-postgres pg_isready -U "${DATABASE_USER:-avatar_user}" &>/dev/null; then
      echo -e " ${GREEN}ready${RESET}"
      break
    fi
    echo -n "."; sleep 2
    [[ $i -eq 30 ]] && echo -e " ${RED}timeout${RESET}"
  done

  echo -n "  Redis    "
  for i in $(seq 1 20); do
    if docker exec avatar-redis redis-cli ping &>/dev/null; then
      echo -e " ${GREEN}ready${RESET}"
      break
    fi
    echo -n "."; sleep 2
    [[ $i -eq 20 ]] && echo -e " ${RED}timeout${RESET}"
  done

  echo -n "  Backend  "
  for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/health &>/dev/null; then
      echo -e " ${GREEN}ready${RESET}"
      break
    fi
    echo -n "."; sleep 3
    [[ $i -eq 30 ]] && echo -e " ${YELLOW}still starting${RESET}"
  done

  echo -n "  Frontend "
  for i in $(seq 1 30); do
    if curl -sf http://localhost:3000 &>/dev/null; then
      echo -e " ${GREEN}ready${RESET}"
      break
    fi
    echo -n "."; sleep 3
    [[ $i -eq 30 ]] && echo -e " ${YELLOW}still starting${RESET}"
  done

  echo ""
  success "All services started!"
  echo ""
  echo -e "  ${BOLD}URLs${RESET}"
  echo -e "  ${GREEN}►${RESET} Frontend     →  ${CYAN}http://localhost:3000${RESET}"
  echo -e "  ${GREEN}►${RESET} Backend API  →  ${CYAN}http://localhost:8000${RESET}"
  echo -e "  ${GREEN}►${RESET} Swagger Docs →  ${CYAN}http://localhost:8000/docs${RESET}"
  echo -e "  ${GREEN}►${RESET} Celery Flower→  ${CYAN}http://localhost:5555${RESET}"
  echo -e "  ${GREEN}►${RESET} Prometheus   →  ${CYAN}http://localhost:9090${RESET}"
  echo ""
  echo -e "  ${BOLD}Useful commands${RESET}"
  echo -e "  ./start.sh --logs    → tail all logs"
  echo -e "  ./start.sh --status  → check service health"
  echo -e "  ./start.sh --stop    → stop everything"
  echo ""

  # Auto-open browser if available
  if command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:3000 &>/dev/null &
  elif command -v open &>/dev/null; then
    open http://localhost:3000 &>/dev/null &
  fi

  exit 0
fi

# =============================================================================
#  DEV MODE  (no Docker — manual processes)
# =============================================================================
if [[ "$MODE" == "dev" ]]; then

  step "Dev mode — checking requirements"

  # Python
  if ! command -v python3 &>/dev/null; then
    error "python3 not found. Install Python 3.11+."
    exit 1
  fi
  PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  success "Python $PY_VER"

  # Node
  if ! command -v node &>/dev/null; then
    error "node not found. Install Node.js 18+."
    exit 1
  fi
  success "Node $(node --version)"

  # Check postgres & redis are reachable
  if ! pg_isready -h "${DATABASE_HOST:-localhost}" -p "${DATABASE_PORT:-5432}" &>/dev/null; then
    warn "PostgreSQL not reachable on localhost:5432. Start it manually or use Docker."
  else
    success "PostgreSQL reachable"
  fi

  if ! redis-cli -h "${REDIS_HOST:-localhost}" ping &>/dev/null; then
    warn "Redis not reachable on localhost:6379. Start it manually or use Docker."
  else
    success "Redis reachable"
  fi

  # Backend venv
  step "Setting up backend"
  cd backend
  if [[ ! -d "venv" ]]; then
    info "Creating Python virtual environment..."
    python3 -m venv venv
  fi
  source venv/bin/activate
  info "Installing backend dependencies..."
  pip install -r requirements.txt -q
  info "Running database migrations..."
  alembic upgrade head 2>/dev/null || warn "Migration failed — DB may not be ready yet"

  # Start backend
  info "Starting backend on :8000"
  uvicorn main:app --reload --port 8000 --host 0.0.0.0 &
  BACKEND_PID=$!
  deactivate

  # Start celery worker in background
  source venv/bin/activate
  info "Starting Celery worker"
  celery -A app.celery_app worker --loglevel=warning --concurrency=2 &
  CELERY_PID=$!
  deactivate
  cd ..

  # Frontend
  step "Setting up frontend"
  cd frontend
  if [[ ! -d "node_modules" ]]; then
    info "Installing frontend dependencies (npm install)..."
    npm install
  fi
  info "Starting frontend on :3000"
  npm run dev &
  FRONTEND_PID=$!
  cd ..

  step "Waiting for services"
  echo -n "  Backend  "
  for i in $(seq 1 20); do
    if curl -sf http://localhost:8000/health &>/dev/null; then
      echo -e " ${GREEN}ready${RESET}"; break
    fi
    echo -n "."; sleep 2
    [[ $i -eq 20 ]] && echo -e " ${YELLOW}still starting...${RESET}"
  done

  echo -n "  Frontend "
  for i in $(seq 1 20); do
    if curl -sf http://localhost:3000 &>/dev/null; then
      echo -e " ${GREEN}ready${RESET}"; break
    fi
    echo -n "."; sleep 3
    [[ $i -eq 20 ]] && echo -e " ${YELLOW}still starting...${RESET}"
  done

  echo ""
  success "Dev server running!"
  echo ""
  echo -e "  ${GREEN}►${RESET} Frontend  →  ${CYAN}http://localhost:3000${RESET}"
  echo -e "  ${GREEN}►${RESET} Backend   →  ${CYAN}http://localhost:8000${RESET}"
  echo -e "  ${GREEN}►${RESET} API Docs  →  ${CYAN}http://localhost:8000/docs${RESET}"
  echo ""
  echo -e "  Press ${BOLD}Ctrl+C${RESET} to stop all services"
  echo ""

  # Auto-open browser
  if command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:3000 &>/dev/null &
  elif command -v open &>/dev/null; then
    open http://localhost:3000 &>/dev/null &
  fi

  # Wait and clean up on Ctrl+C
  trap "echo ''; step 'Stopping dev services'; kill $BACKEND_PID $FRONTEND_PID $CELERY_PID 2>/dev/null; success 'Stopped.'; exit 0" INT TERM
  wait
fi
