<div align="center">

<h1>🎭 AvatarAI — Real-Time AI Avatar Platform</h1>

<p><strong>Upload a photo · Clone a voice · Talk to any face in real time</strong></p>

<p>
  <a href="https://github.com/PunithVT/ai-avatar-system/stargazers"><img src="https://img.shields.io/github/stars/PunithVT/ai-avatar-system?style=for-the-badge&color=7c3aed" alt="Stars"/></a>
  <a href="https://github.com/PunithVT/ai-avatar-system/forks"><img src="https://img.shields.io/github/forks/PunithVT/ai-avatar-system?style=for-the-badge&color=3b82f6" alt="Forks"/></a>
  <a href="https://github.com/PunithVT/ai-avatar-system/issues"><img src="https://img.shields.io/github/issues/PunithVT/ai-avatar-system?style=for-the-badge" alt="Issues"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge" alt="MIT License"/></a>
</p>

<p>
  <img src="https://img.shields.io/badge/Next.js-14-black?logo=next.js&style=flat-square" />
  <img src="https://img.shields.io/badge/FastAPI-0.109-009688?logo=fastapi&style=flat-square" />
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&style=flat-square" />
  <img src="https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&style=flat-square" />
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&style=flat-square" />
  <img src="https://img.shields.io/badge/CUDA-11.8-76B900?logo=nvidia&style=flat-square" />
  <img src="https://img.shields.io/badge/PostgreSQL-15-336791?logo=postgresql&style=flat-square" />
  <img src="https://img.shields.io/badge/Redis-7-DC382D?logo=redis&style=flat-square" />
</p>

<p>
  <a href="#-quick-start">Quick Start</a> ·
  <a href="#-features">Features</a> ·
  <a href="#-architecture">Architecture</a> ·
  <a href="#-gpu--aws-deployment">GPU / AWS Deploy</a> ·
  <a href="#-api-reference">API</a> ·
  <a href="#-roadmap">Roadmap</a>
</p>

> **The most complete open-source AI talking avatar system.**
> Real-time lip-sync · Zero-shot voice cloning · Multi-LLM · Runs 100% locally or on AWS.

</div>

---

## 🎬 What is AvatarAI?

AvatarAI is an open-source, production-ready platform for building **photorealistic AI avatar conversations**. Upload any face photo, clone a voice from a 5-second audio clip, and have a real-time conversation — with **lip-sync video generated on every single response**.

```
[mic input]  →  Whisper STT  →  Claude / GPT-4  →  XTTS v2 TTS  →  MuseTalk lip-sync  →  [video]
                                 < 2–4 s first chunk on AWS GPU >
```

**What makes AvatarAI different:**
- 🎤 **Zero-shot voice cloning** — 5 seconds of audio is all you need (XTTS v2)
- 🎭 **Any face, any language** — upload a JPEG, pick from 18 languages, start talking
- ⚡ **Sentence-chunk streaming** — first video chunk plays while the rest is still being generated
- 😴 **Idle animation** — avatar breathes and glows while waiting, no blank screens
- 🔒 **100% local mode** — nothing leaves your machine
- 🔌 **Multi-LLM** — Claude, GPT-4, or Llama 3 (free, local via Ollama)
- 🚀 **AWS GPU deployment** — one-command deploy to `g5.xlarge` for true real-time (~30 FPS)
- 🏗️ **Production-ready** — JWT auth, rate limiting, S3 storage, Terraform IaC

---

## ✨ Features

| Category | Details |
|---|---|
| 🤖 **LLM Backends** | Claude (Anthropic) · GPT-4o (OpenAI) · Llama 3 (Ollama, local) |
| 🎤 **Voice Cloning** | Record 5–30 s → XTTS v2 zero-shot speaker embedding |
| 🗣️ **Speech-to-Text** | OpenAI Whisper (`faster-whisper`, CUDA-accelerated), 18+ languages |
| 🎬 **Lip-Sync Video** | MuseTalk V1.5 (30 FPS on GPU) · FFmpeg fallback (CPU) |
| ⚡ **Streaming Pipeline** | Sentence chunks stream over WebSocket as they complete |
| 😴 **Idle Animation** | CSS breathing animation while avatar waits — no blank screens |
| 😊 **Emotion Detection** | Live emotion badges per message |
| 🌍 **18+ Languages** | Whisper multilingual STT + XTTS v2 multilingual TTS |
| 🏠 **Local-First Storage** | `USE_LOCAL_STORAGE=true` — no AWS needed for dev |
| 🔐 **Auth & Sessions** | JWT authentication, conversation history, persistent sessions |
| 📊 **Observability** | Prometheus · Celery Flower · Sentry · structured logging |
| 🧪 **Tested** | Full pytest suite — users, avatars, sessions, health checks |
| 🚀 **AWS GPU Deploy** | One-command `g5.xlarge` deploy with CUDA 11.8 + float16 |

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        Browser / Client                       │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │Avatar Studio│  │ Voice Studio │  │   Chat Interface     │ │
│  │  (upload)   │  │  (cloning)   │  │ Idle anim + chunks   │ │
│  └──────┬──────┘  └──────┬───────┘  └──────────┬───────────┘ │
└─────────┼───────────────┼─────────────────────┼─────────────┘
          │ REST           │ REST                │ WebSocket
          ▼                ▼                     ▼
┌──────────────────────────────────────────────────────────────┐
│                       FastAPI Backend                         │
│  ┌──────────────────────────────────────────────────────┐    │
│  │                  WebSocket Manager                    │    │
│  │  split sentences → TTS → MuseTalk → stream chunks    │    │
│  └──────────────────────────────────────────────────────┘    │
│  ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌───────────────┐  │
│  │  Whisper │ │Claude/GPT │ │ XTTS v2  │ │  MuseTalk     │  │
│  │   STT    │ │  / Llama  │ │   TTS    │ │  (GPU/CPU)    │  │
│  └──────────┘ └───────────┘ └──────────┘ └───────────────┘  │
│  ┌──────────┐ ┌──────────┐  ┌──────────┐ ┌───────────────┐  │
│  │PostgreSQL│ │  Redis   │  │  Celery  │ │ Local FS / S3 │  │
│  └──────────┘ └──────────┘  └──────────┘ └───────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### Real-Time Data Flow (one conversation turn)

```
[User types / speaks]
        │
        ▼
  Whisper STT ─────────────────► transcript
        │
        ▼
  Claude / GPT / Llama ────────► full response text
        │
        ▼
  Split into sentences ────────► ["Hello!", "How are you?", ...]
        │
        ├── sentence 1 → XTTS → MuseTalk → video_chunk WS → browser plays
        ├── sentence 2 → XTTS → MuseTalk → video_chunk WS → queued
        └── sentence N → XTTS → MuseTalk → video_chunk WS → queued
```

---

## 📁 Project Structure

```
ai-avatar-system/
├── backend/                    # FastAPI application
│   ├── app/
│   │   ├── api/v1/             # REST endpoints (users, avatars, sessions, messages)
│   │   ├── services/           # Core services (LLM, TTS, STT, animator, storage)
│   │   ├── models/             # SQLAlchemy DB models
│   │   └── websocket.py        # Real-time WebSocket handler + sentence streaming
│   ├── alembic/                # Database migrations
│   ├── models/MuseTalk/        # MuseTalk V1.5 (lip-sync engine)
│   │   └── scripts/
│   │       └── musetalk_worker.py  # Persistent worker (models loaded once)
│   ├── tests/                  # pytest suite
│   ├── Dockerfile              # CUDA 11.8 base image
│   └── requirements.txt
├── frontend/                   # Next.js 14 application
│   ├── app/                    # App Router pages
│   ├── components/             # React components (ChatInterface, IdleAvatar, etc.)
│   ├── lib/api.ts              # Axios API client
│   └── store/                  # Zustand global state
├── nginx/
│   └── nginx.conf              # Reverse proxy (HTTP → backend/frontend, WebSocket)
├── infrastructure/
│   ├── main.tf                 # AWS Terraform (ECS, RDS, ElastiCache, S3, CloudFront)
│   └── variables.tf
├── scripts/
│   ├── setup_musetalk.sh       # Download MuseTalk models (~9 GB)
│   └── deploy-aws.sh           # One-command EC2 GPU deployment
├── docker-compose.yml          # Development (CPU) — all services
├── docker-compose.prod.yml     # Production overrides (GPU, no bind mounts, logging)
├── deploy.sh                   # ECR push + Terraform deploy (ECS path)
├── .env.example                # Development env template
└── .env.prod.example           # Production env template
```

---

## 🚀 Quick Start

### Prerequisites

- **Docker & Docker Compose** v2+ (recommended)
- OR: Python 3.10+, Node.js 18+, FFmpeg, PostgreSQL, Redis

### Option A — Docker / CPU (development)

```bash
git clone https://github.com/PunithVT/ai-avatar-system.git
cd ai-avatar-system
cp .env.example .env          # add your ANTHROPIC_API_KEY (or OPENAI_API_KEY)
docker compose up -d
```

| Service | URL |
|---|---|
| 🖥️ Frontend | http://localhost:3000 |
| ⚙️ Backend API | http://localhost:8000 |
| 📖 Swagger Docs | http://localhost:8000/docs |
| 🌸 Celery Flower | http://localhost:5555 |

> **No AWS required.** Set `USE_LOCAL_STORAGE=true` (default) — uploads saved to `backend/uploads/`.

### Option B — Manual (development)

```bash
# Backend
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env
alembic upgrade head
uvicorn main:app --reload --port 8000

# Frontend (new terminal)
cd frontend
npm install
npm run dev
```

### Option C — Enable MuseTalk Lip-Sync

```bash
# Download models (~9 GB, one-time)
bash scripts/setup_musetalk.sh

# Set in .env
AVATAR_ENGINE=musetalk

# Restart
docker compose restart backend
```

---

## 🚀 GPU & AWS Deployment

MuseTalk achieves **30 FPS at 256×256 on a V100-class GPU** (source: [MuseTalk paper](https://arxiv.org/abs/2410.10122)). On CPU it is 30–50× slower. Deploying on AWS gets you genuine real-time performance.

### Recommended Instance

| Instance | GPU | VRAM | Spot $/hr | MuseTalk FPS |
|---|---|---|---|---|
| `g4dn.xlarge` | T4 | 16 GB | ~$0.16 | ~15–20 FPS |
| `g5.xlarge` | A10G | 24 GB | ~$0.30 | **~30 FPS** ✓ |
| `g6.xlarge` | L4 | 24 GB | ~$0.24 | **~30 FPS** ✓ |

**Recommended: `g5.xlarge` Spot** (~$72/mo at 8 hrs/day).

### One-Command EC2 Deploy

```bash
# 1. Launch g5.xlarge with Ubuntu 22.04 LTS, SSH in, then:
bash <(curl -fsSL https://raw.githubusercontent.com/PunithVT/ai-avatar-system/main/scripts/deploy-aws.sh)

# 2. Fill in API keys:
nano /opt/ai-avatar-system/.env.prod

# 3. Redeploy with your keys:
bash /opt/ai-avatar-system/scripts/deploy-aws.sh --update
```

The script automatically:
- Installs Docker + nvidia-docker2
- Verifies GPU is accessible
- Downloads MuseTalk models (~9 GB)
- Starts all services with GPU passthrough + float16 (2× faster via Tensor Cores)

### Manual Production Docker

```bash
cp .env.prod.example .env.prod   # fill in your values
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

**What `docker-compose.prod.yml` adds over development:**
- GPU reservation (`nvidia` driver, count=1) for backend + celery-worker
- `float16` inference enabled automatically on CUDA → ~2× speedup
- Persistent `musetalk_models` volume (survive container restarts)
- No source-code bind mounts (runs from built image)
- Log rotation (100 MB max, 5 files)
- Flower disabled (security)

### Verify GPU is Working

```bash
# Check GPU is visible in container
docker exec avatar-backend python -c "
import torch
print('CUDA:', torch.cuda.is_available())
print('GPU:', torch.cuda.get_device_name(0))
print('VRAM:', round(torch.cuda.get_device_properties(0).total_memory/1024**3,1), 'GB')
"

# Expected on g5.xlarge:
# CUDA: True
# GPU: NVIDIA A10G
# VRAM: 24.0 GB

# Live GPU utilisation
docker exec avatar-backend nvidia-smi
```

### AWS Terraform (ECS Path)

For a fully managed ECS deployment with RDS + ElastiCache + CloudFront:

```bash
cd infrastructure
terraform init
terraform apply -var="environment=production"
bash deploy.sh production
```

---

## 🎤 Voice Cloning

Powered by [XTTS v2](https://github.com/coqui-ai/TTS) — zero-shot voice cloning from 5 seconds of audio.

1. Go to **Voice** tab → **Clone Voice**
2. Record 5–30 s of clear speech (or upload a WAV/MP3)
3. Name it → **Clone** → select it for your session

Every TTS response then uses your cloned voice.

```bash
# REST API
curl -X POST http://localhost:8000/api/v1/voices/clone \
  -F "audio=@my_voice.wav" -F "name=My Voice" -F "language=en"
```

---

## 📡 API Reference

### Authentication

```bash
POST /api/v1/users/register   { "email": "...", "username": "...", "password": "..." }
POST /api/v1/users/login      form: username=... password=...   → { "access_token": "..." }

# All protected routes:
Authorization: Bearer <access_token>
```

### Avatars

```
POST   /api/v1/avatars/upload        Upload photo (multipart: file + name)
GET    /api/v1/avatars/              List avatars
DELETE /api/v1/avatars/{id}          Delete avatar
PUT    /api/v1/avatars/{id}/voice    Assign voice to avatar
```

### Sessions & Messages

```
POST   /api/v1/sessions/create       { "avatar_id": "..." }
POST   /api/v1/sessions/{id}/end
GET    /api/v1/messages/session/{id}
```

### WebSocket

```
WS  /ws/session/{session_id}
```

**Client → Server:**
```json
{ "type": "text",      "text": "Hello!" }
{ "type": "audio",     "audio": "<base64-webm>" }
{ "type": "set_voice", "voice_wav_path": "/path/to/speaker.wav" }
```

**Server → Client:**
```json
{ "type": "transcription",   "text": "Hello!" }
{ "type": "message",         "content": "Hi!", "role": "assistant" }
{ "type": "video_chunk_start","total_chunks": 3 }
{ "type": "video_chunk",     "chunk_index": 0, "video_url": "...", "text": "Hi!" }
{ "type": "video_chunk_end" }
{ "type": "status",          "message": "Animating part 1 of 3…" }
{ "type": "error",           "message": "Something went wrong" }
```

---

## ⚙️ Configuration

Key `.env` variables:

```bash
# LLM
LLM_PROVIDER=anthropic            # anthropic | openai | ollama
LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-...

# Avatar engine
AVATAR_ENGINE=musetalk            # musetalk (GPU recommended) | simple (CPU fallback)
MUSETALK_PATH=models/MuseTalk

# TTS
TTS_PROVIDER=coqui                # coqui (XTTS v2) | elevenlabs
ELEVENLABS_API_KEY=...

# STT
WHISPER_MODEL=base                # tiny | base | small | medium | large-v3

# Storage
USE_LOCAL_STORAGE=true            # false → AWS S3
S3_BUCKET_NAME=...

# Auth
SECRET_KEY=change-me-in-production
JWT_EXPIRATION_HOURS=24
```

---

## 🛠️ Tech Stack

### Frontend
| Library | Purpose |
|---|---|
| Next.js 14 + React 18 | App framework |
| TypeScript 5 | Type safety |
| Tailwind CSS | Styling |
| Zustand | Global state |

### Backend
| Library | Purpose |
|---|---|
| FastAPI | Async REST API + WebSocket |
| SQLAlchemy 2 (async) | ORM with asyncpg |
| PostgreSQL 15 | Primary database |
| Alembic | Migrations |
| Redis 7 | Cache + Celery broker |
| Celery | Background tasks |

### AI / ML
| Model | Purpose |
|---|---|
| Claude / GPT-4o / Llama 3 | LLM conversation |
| Whisper (`faster-whisper`) | Speech-to-text |
| XTTS v2 (Coqui TTS) | TTS + zero-shot voice cloning |
| MuseTalk V1.5 | Photorealistic lip-sync (30 FPS on GPU) |

---

## 🧪 Running Tests

```bash
cd backend
pytest -v                           # all tests
pytest tests/test_health.py         # single module
pytest --cov=app --cov-report=html  # HTML coverage
```

---

## 🗺️ Roadmap

- [ ] **Streaming LLM** — start TTS before LLM finishes (token-by-token)
- [ ] **Emotion-driven animation** — detected emotion changes facial expression
- [ ] **Embeddable widget** — drop a talking avatar into any website with 3 lines of JS
- [ ] **Multi-avatar conversations** — two avatars talking to each other
- [ ] **Long-term memory** — RAG + vector DB for persistent context
- [ ] **Mobile app** — React Native iOS/Android client
- [ ] **Video call integration** — replace your face in Zoom/Meet

---

## ❓ FAQ

**Q: Do I need a GPU?**
A: No — everything runs on CPU. MuseTalk takes 30–90 s/sentence on CPU. For real-time performance, use an AWS `g5.xlarge` (~$0.30/hr spot).

**Q: Can I run it with no API key?**
A: Yes — set `LLM_PROVIDER=ollama` and run [Ollama](https://ollama.ai) locally. Fully offline and free.

**Q: How do I get MuseTalk models?**
A: Run `bash scripts/setup_musetalk.sh` — downloads ~9 GB of models automatically.

**Q: Why does the first response take longer?**
A: The MuseTalk persistent worker loads all models into GPU VRAM on the first request (~60 s on GPU, ~5 min on CPU). Subsequent requests are fast.

**Q: What avatar photo works best?**
A: A clear, well-lit frontal face photo (JPEG/PNG/WebP). Avoid sunglasses or heavy occlusion.

---

## 🤝 Contributing

Contributions welcome! Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.

```bash
git clone https://github.com/PunithVT/ai-avatar-system.git
git checkout -b feat/my-feature
# make changes + tests
git commit -m "feat(backend): add my feature"
git push origin feat/my-feature
```

---

## 📄 License

MIT © 2025 — see [LICENSE](LICENSE) for details.

---

<div align="center">

**If AvatarAI saves you time or inspires your project, please ⭐ star the repo.**

<a href="https://github.com/PunithVT/ai-avatar-system/stargazers">
  <img src="https://img.shields.io/github/stars/PunithVT/ai-avatar-system?style=social" />
</a>

<br/><br/>

<sub>Built with FastAPI · Next.js · MuseTalk V1.5 · XTTS v2 · Whisper · Claude AI</sub>

</div>
