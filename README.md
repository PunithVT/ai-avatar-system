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
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&style=flat-square" />
  <img src="https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&style=flat-square" />
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&style=flat-square" />
  <img src="https://img.shields.io/badge/Redis-7-DC382D?logo=redis&style=flat-square" />
  <img src="https://img.shields.io/badge/PostgreSQL-15-336791?logo=postgresql&style=flat-square" />
  <img src="https://img.shields.io/badge/CI-passing-brightgreen?logo=github-actions&style=flat-square" />
</p>

<p>
  <a href="#-quick-start">Quick Start</a> ·
  <a href="#-features">Features</a> ·
  <a href="#-architecture">Architecture</a> ·
  <a href="#-voice-cloning">Voice Cloning</a> ·
  <a href="#-api-reference">API</a> ·
  <a href="#-deployment">Deploy</a> ·
  <a href="#-roadmap">Roadmap</a> ·
  <a href="#-contributing">Contribute</a>
</p>

> **The most complete open-source AI talking avatar system.**
> Real-time lip-sync · Zero-shot voice cloning · Multi-LLM · Runs 100% locally.

</div>

---

## 🎬 What is AvatarAI?

AvatarAI is an open-source, production-ready platform for building **photorealistic AI avatar conversations**. Upload any face photo, clone a voice from a 5-second audio clip, and have a real-time conversation — with **lip-sync video generated on every single response**.

```
[mic input]  →  Whisper STT  →  Claude / GPT-4 / Llama  →  XTTS v2 TTS  →  MuseTalk lip-sync  →  [video]
                                  < 200 ms end-to-end on GPU >
```

**What makes AvatarAI different:**
- 🎤 **Zero-shot voice cloning** — 5 seconds of audio is all you need (XTTS v2)
- 🎭 **Any face, any language** — upload a JPEG, pick from 18 languages, start talking
- ⚡ **True real-time WebSocket pipeline** — no polling, no page reloads
- 🔒 **100% local mode** — nothing leaves your machine
- 🔌 **3 LLM backends** — Claude, GPT-4, or Llama 3 via Ollama (free, local)
- 🏗️ **Production-ready** — JWT auth, rate limiting, Prometheus, Sentry, CI/CD

---

## ✨ Features

| Category | Details |
|---|---|
| 🤖 **LLM Backends** | Claude (Anthropic) · GPT-4o (OpenAI) · Llama 3 (Ollama, local) |
| 🎤 **Voice Cloning** | Record 5–30 s → XTTS v2 zero-shot speaker embedding, applied every TTS call |
| 🗣️ **Speech-to-Text** | OpenAI Whisper (`faster-whisper`, CUDA-accelerated), 18+ languages |
| 🎬 **Lip-Sync Video** | MuseTalk · Simple fallback — photorealistic, every response |
| ⚡ **Real-Time Pipeline** | WebSocket: STT → LLM → TTS → Animator → video in one continuous pass |
| 😊 **Emotion Detection** | Live emotion badges per message (happy · angry · sad · excited · curious) |
| 🌍 **18+ Languages** | Whisper multilingual STT + XTTS v2 multilingual TTS |
| 🏠 **Local-First Storage** | `USE_LOCAL_STORAGE=true` — files served from `uploads/`, no AWS needed |
| 🔐 **Auth & Sessions** | JWT authentication, conversation history, persistent sessions |
| 📊 **Observability** | Prometheus metrics · Celery Flower · Sentry error tracking · request logging |
| 🧪 **Tested** | Full pytest suite — users, avatars, sessions, health checks |
| 🚀 **CI/CD Ready** | GitHub Actions: lint + test + Docker build + deploy |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Browser / Client                            │
│  ┌───────────────┐  ┌────────────────┐  ┌────────────────────────┐  │
│  │ Avatar Studio │  │  Voice Studio  │  │    Chat Interface      │  │
│  │  (upload)     │  │  (cloning)     │  │  WebSocket + video     │  │
│  └───────┬───────┘  └───────┬────────┘  └──────────┬─────────────┘  │
└──────────┼─────────────────┼───────────────────────┼────────────────┘
           │  REST            │  REST                 │  WebSocket
           ▼                  ▼                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          FastAPI Backend                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐    │
│  │ /avatars │  │ /voices  │  │ /sessions│  │   /messages      │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘    │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │                     WebSocket Manager                        │    │
│  └──────────────────────────────────────────────────────────────┘    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐    │
│  │ Whisper  │  │ Claude / │  │ XTTS v2  │  │  MuseTalk /     │    │
│  │   STT    │  │ GPT/Llama│  │   TTS    │  │  MuseTalk   │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐    │
│  │PostgreSQL│  │  Redis   │  │  Celery  │  │  Local FS / S3   │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

### One conversation turn — data flow

```
[Mic / Text Input]
      │
      ▼
 Whisper STT ──────────────► transcript text
      │
      ▼
 Claude / GPT-4 / Llama ───► assistant response
      │
      ▼
 XTTS v2 TTS               ► audio WAV
 (+ cloned speaker_wav)
      │
      ▼
 MuseTalk ► lip-sync MP4
      │
      ▼
 WebSocket push ───────────► browser <video> element
```

---

## 🚀 Quick Start

### Prerequisites

- **Docker & Docker Compose** (recommended — one command setup)
- OR: Python 3.11+, Node.js 18+, FFmpeg, PostgreSQL, Redis

### Option A — Docker (recommended)

```bash
git clone https://github.com/PunithVT/ai-avatar-system.git
cd ai-avatar-system
cp .env.example .env          # set your ANTHROPIC_API_KEY (or OPENAI_API_KEY)
docker compose up -d
```

| Service | URL |
|---|---|
| 🖥️ Frontend | http://localhost:3000 |
| ⚙️ Backend API | http://localhost:8000 |
| 📖 Swagger Docs | http://localhost:8000/docs |
| 🌸 Celery Flower | http://localhost:5555 |
| 📊 Prometheus | http://localhost:9090 |

### Option B — Manual (development)

```bash
# Backend
cd backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                              # fill in your API key
alembic upgrade head                              # run DB migrations
uvicorn main:app --reload --port 8000

# Frontend (new terminal)
cd frontend
npm install
npm run dev
```

Open **http://localhost:3000**

> **No AWS required.** All uploads and generated videos are saved to `backend/uploads/` and served at `http://localhost:8000/uploads/` by default.

---

## 🎤 Voice Cloning

AvatarAI ships a full **Voice Studio** powered by [XTTS v2](https://github.com/coqui-ai/TTS) — state-of-the-art zero-shot voice cloning.

**Clone a voice in 3 steps:**
1. Navigate to the **Voice** tab → click **Clone Voice**
2. Record 5–30 seconds of clear speech (or upload a WAV/MP3)
3. Name it → click **Clone This Voice** → select it for your session

Once selected, every TTS response uses your cloned voice. Voice selection is sent to the backend via the WebSocket `set_voice` message.

### Voice REST API

```bash
# Clone a voice from audio
curl -X POST http://localhost:8000/api/v1/voices/clone \
  -F "audio=@my_voice.wav" -F "name=My Voice" -F "language=en"

# List all voice profiles
curl http://localhost:8000/api/v1/voices/

# Delete a voice profile
curl -X DELETE http://localhost:8000/api/v1/voices/{voice_id}
```

---

## 📡 API Reference

### Authentication

```bash
# Register a new user
POST /api/v1/users/register
{ "email": "user@example.com", "username": "alice", "password": "secret" }

# Login (returns JWT access token)
POST /api/v1/users/login    (form: username=... password=...)

# Use token on all protected routes
Authorization: Bearer <access_token>
```

### Avatars

```
POST   /api/v1/avatars/upload        Upload avatar image (multipart: file + name)
GET    /api/v1/avatars/              List all avatars
GET    /api/v1/avatars/{id}          Get avatar details
DELETE /api/v1/avatars/{id}          Delete avatar
```

### Voice Cloning

```
POST   /api/v1/voices/clone          Clone voice from audio sample
GET    /api/v1/voices/               List all voice profiles
GET    /api/v1/voices/{id}           Get voice profile
DELETE /api/v1/voices/{id}           Delete voice profile
```

### Sessions & Messages

```
POST   /api/v1/sessions/create       Create session  { "avatar_id": "..." }
POST   /api/v1/sessions/{id}/end     End a session
GET    /api/v1/messages/session/{id} Get conversation history
POST   /api/v1/messages/send         Send a message (REST fallback)
```

### WebSocket Real-Time Channel

```
WS  /ws/session/{session_id}
```

**Client → Server:**
```json
{ "type": "text",      "text": "Hello!" }
{ "type": "audio",     "audio": "<base64-encoded-webm>" }
{ "type": "set_voice", "voice_wav_path": "/path/to/speaker.wav" }
{ "type": "ping" }
```

**Server → Client:**
```json
{ "type": "transcription", "text": "Hello!" }
{ "type": "message",       "content": "Hi there!", "role": "assistant" }
{ "type": "video",         "video_url": "http://localhost:8000/uploads/video.mp4" }
{ "type": "status",        "message": "Generating response…", "stage": "llm" }
{ "type": "error",         "message": "Something went wrong" }
{ "type": "pong" }
```

---

## ⚙️ Configuration

All settings are in `.env`. Key variables:

```bash
# ── LLM ──────────────────────────────────────────────────────────────
LLM_PROVIDER=anthropic            # anthropic | openai | ollama
LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# ── Avatar Animation Engine ───────────────────────────────────────────
AVATAR_ENGINE=musetalk           # musetalk | simple

# ── TTS ──────────────────────────────────────────────────────────────
TTS_PROVIDER=coqui                # coqui (XTTS v2) | elevenlabs | bark
ELEVENLABS_API_KEY=...

# ── STT ──────────────────────────────────────────────────────────────
WHISPER_MODEL=base                # tiny | base | small | medium | large-v3

# ── Storage ──────────────────────────────────────────────────────────
USE_LOCAL_STORAGE=true            # false = AWS S3
LOCAL_STORAGE_PATH=uploads
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
S3_BUCKET_NAME=...

# ── Auth ─────────────────────────────────────────────────────────────
SECRET_KEY=change-me-in-production
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# ── Observability ────────────────────────────────────────────────────
SENTRY_DSN=https://...@sentry.io/...
PROMETHEUS_ENABLED=true
LOG_LEVEL=INFO
```

---

## 🛠️ Tech Stack

### Frontend
| Library | Purpose |
|---|---|
| Next.js 14 + React 18 | App framework |
| TypeScript 5 | Type safety |
| Tailwind CSS | Styling |
| TanStack Query | Server state |
| Zustand | Global client state |
| Canvas API | Real-time waveform visualizer |

### Backend
| Library | Purpose |
|---|---|
| FastAPI | Async REST API + WebSocket |
| SQLAlchemy 2 (async) | ORM with asyncpg |
| PostgreSQL 15 | Primary database |
| Alembic | Database migrations |
| Redis 7 | Cache + task queue |
| Celery | Background tasks |
| Prometheus + Sentry | Metrics + error tracking |

### AI / ML
| Model | Purpose |
|---|---|
| Claude / GPT-4o / Llama 3 | LLM conversation |
| Whisper (`faster-whisper`) | Speech-to-text |
| XTTS v2 (Coqui TTS) | Text-to-speech + zero-shot voice cloning |
| MuseTalk V1.5 | Photorealistic lip-sync video generation |
| MuseTalk | Alternative lip-sync animator |

---

## 🚢 Deployment

### Self-hosted Docker

```bash
docker compose -f docker-compose.prod.yml up -d
```

### AWS (ECS Fargate + Terraform)

```bash
cd infrastructure
terraform init
terraform apply -var="environment=production"
./deploy.sh production
```

Set `USE_LOCAL_STORAGE=false` and add S3 credentials for cloud storage in production.

---

## 🧪 Running Tests

```bash
cd backend
pip install -r requirements.txt
pytest -v                        # run all tests
pytest tests/test_health.py      # specific module
pytest --cov=app --cov-report=html  # with HTML coverage report
```

---

## 🗺️ Roadmap

- [ ] **Embeddable avatar widget** — drop a talking avatar into any website with 3 lines of JS
- [ ] **Streaming LLM** — start TTS before LLM finishes (token-by-token)
- [ ] **Emotion-driven animation** — detected emotion changes the avatar's facial expression
- [ ] **Multi-avatar conversations** — two avatars talking to each other
- [ ] **Long-term memory** — RAG + vector DB for persistent conversation context
- [ ] **Mobile app** — React Native iOS/Android client
- [ ] **Avatar marketplace** — share and download community-created avatars
- [ ] **Video call integration** — replace your face in a live Zoom/Meet call

---

## ❓ FAQ

**Q: Do I need a GPU?**
A: No — everything runs on CPU (slower). An NVIDIA GPU with 8GB+ VRAM is recommended for real-time performance (~200ms latency).

**Q: Can I use it with no API key at all?**
A: Yes — set `LLM_PROVIDER=ollama` and run [Ollama](https://ollama.ai) locally. Fully offline, fully free.

**Q: How long does voice cloning take?**
A: XTTS v2 creates the speaker embedding in ~2 seconds from a 10-second sample. Each TTS call is then ~500ms on GPU.

**Q: What file formats are supported for avatar photos?**
A: JPEG, PNG, WebP. A clear frontal face photo gives the best lip-sync results.

**Q: Is this production-ready?**
A: The platform includes JWT auth, rate limiting, security headers, Sentry error tracking, Prometheus metrics, Alembic migrations, and a full test suite. Ready for private/internal production deployment.

---

## 🤝 Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR. This project follows [Conventional Commits](https://www.conventionalcommits.org/).

```bash
# Fork & clone
git clone https://github.com/YOUR_USERNAME/ai-avatar-system.git
cd ai-avatar-system

# Create a feature branch
git checkout -b feat/my-feature

# Make changes, write tests, commit
git commit -m "feat(backend): add my feature"

# Push & open a pull request
git push origin feat/my-feature
```

Look for issues tagged `good first issue` to get started.

---

## 📄 License

MIT © 2025 — see [LICENSE](LICENSE) for details.

---

<div align="center">

**If AvatarAI saves you time or inspires your project, please ⭐ star the repo — it helps others find it.**

<a href="https://github.com/PunithVT/ai-avatar-system/stargazers">
  <img src="https://img.shields.io/github/stars/PunithVT/ai-avatar-system?style=social" />
</a>

<br/><br/>

<sub>Built with FastAPI · Next.js · MuseTalk · XTTS v2 · Whisper · Claude AI</sub>

</div>
