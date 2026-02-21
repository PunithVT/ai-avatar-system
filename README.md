<div align="center">

# AvatarAI — Real-Time AI Avatar Conversation Platform

[![Next.js](https://img.shields.io/badge/Next.js-16-black?logo=next.js)](https://nextjs.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker)](docker-compose.yml)
[![Claude](https://img.shields.io/badge/Powered%20by-Claude%20AI-7c3aed)](https://anthropic.com)

**Upload a photo · Clone a voice · Talk to any face in real time**

[Features](#features) · [Quick Start](#quick-start) · [Architecture](#architecture) · [API](#api-reference) · [Voice Cloning](#voice-cloning) · [Deploy](#deployment)

</div>

---

## What is AvatarAI?

AvatarAI is an open-source, full-stack platform for building **photorealistic AI avatar conversations**. Upload any face photo, optionally clone a voice from a short audio sample, and have real-time conversations powered by Claude, GPT-4, or a local Ollama model — with **lip-sync video generated on every response**.

Works completely offline and locally — no AWS account, no cloud storage, no external dependencies beyond your LLM API key.

---

## Features

| Feature | Details |
|---|---|
| **LLM Backends** | Claude (Anthropic) · GPT-4 (OpenAI) · Llama 3 (Ollama, local) |
| **Voice Cloning** | Record 5–30 s → XTTS v2 speaker embedding, applied to every TTS response |
| **Speech-to-Text** | Whisper (faster-whisper, CUDA-accelerated) |
| **Lip-Sync Animation** | SadTalker · Live Portrait · Simple (fallback) |
| **Real-Time Pipeline** | WebSocket — STT → LLM → TTS → Animator → video in one pass |
| **Emotion Detection** | Client-side heuristic badges per message |
| **18+ Languages** | Whisper STT + XTTS v2 TTS |
| **Local Storage** | Files served from `uploads/` — no S3 required |
| **Privacy** | 100 % local mode — nothing leaves your machine |
| **Monitoring** | Prometheus metrics · Celery Flower · Sentry |
| **Windows Support** | Cross-platform temp paths, no Unix-only assumptions |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser / Client                          │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ Avatar Studio│  │  Voice Studio│  │    Chat Interface      │ │
│  │  (upload)    │  │  (cloning)   │  │  (WebSocket + video)   │ │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬─────────────┘ │
└─────────┼─────────────────┼──────────────────────┼───────────────┘
          │  REST           │  REST                │  WebSocket
          ▼                 ▼                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI Backend                             │
│  ┌──────────┐  ┌────────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ Avatars  │  │   Voices   │  │ Sessions │  │   Messages   │  │
│  └──────────┘  └────────────┘  └──────────┘  └──────────────┘  │
│                                                                  │
│  ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐  │
│  │  STT    │   │   LLM    │   │   TTS    │   │  Animator    │  │
│  │ Whisper │   │  Claude  │   │  XTTS v2 │   │  SadTalker   │  │
│  └────┬────┘   └────┬─────┘   └────┬─────┘   └──────┬───────┘  │
└───────┼─────────────┼──────────────┼─────────────────┼──────────┘
        │             │              │                 │
   Audio input   Text + context  Audio output    Video output
        └─────────────┴──────────────┴─────────────────┘
                           Local filesystem / uploads/
```

### Data Flow (one conversation turn)

```
Mic/Text → [Whisper STT] → text → [Claude/GPT/Ollama] → response →
[XTTS v2 TTS + cloned voice WAV] → audio → [SadTalker] → lip-sync video →
WebSocket → browser video player → served from /uploads/
```

---

## Quick Start

### Prerequisites

- Docker & Docker Compose (recommended)
- OR: Python 3.11+, Node.js 18+, FFmpeg

### 1. Clone & configure

```bash
git clone https://github.com/your-username/avatar-ai
cd avatar-ai
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY (or OPENAI_API_KEY)
```

### 2. Start with Docker Compose

```bash
docker compose up -d
```

| Service | URL |
|---|---|
| Frontend (Next.js) | http://localhost:3000 |
| Backend (FastAPI) | http://localhost:8000 |
| API Docs (Swagger) | http://localhost:8000/docs |
| Celery Flower | http://localhost:5555 |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

### 3. Start manually (development)

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Frontend (new terminal)
cd frontend
npm install
npm run dev
```

Open http://localhost:3000

> **No AWS required.** By default all uploads and generated videos are saved to the `backend/uploads/` directory and served at `http://localhost:8000/uploads/`.

---

## Voice Cloning

AvatarAI includes a full **Voice Studio** for cloning and managing voice profiles:

1. Navigate to the **Voice** tab in the top nav
2. Click **Clone Voice**
3. Record 5–30 seconds of clear speech
4. Give it a name → **Clone This Voice**
5. Select the cloned voice — it activates for the current chat session automatically

Internally the audio is stored as a WAV reference file and passed to **XTTS v2** as `speaker_wav` on every TTS synthesis call. Voice selection is communicated to the backend via the WebSocket `set_voice` message.

### Voice API

```bash
# Clone a voice from an audio sample
curl -X POST http://localhost:8000/api/v1/voices/clone \
  -F "audio=@sample.wav" \
  -F "name=My Voice" \
  -F "language=en"

# List all voice profiles
curl http://localhost:8000/api/v1/voices/

# Get a voice profile
curl http://localhost:8000/api/v1/voices/{voice_id}

# Delete a voice
curl -X DELETE http://localhost:8000/api/v1/voices/{voice_id}
```

---

## API Reference

### Avatars

```
POST   /api/v1/avatars/upload          Upload avatar image (multipart: file + name)
GET    /api/v1/avatars/                List all avatars
GET    /api/v1/avatars/{id}            Get avatar
DELETE /api/v1/avatars/{id}            Delete avatar
```

### Voice Cloning

```
POST   /api/v1/voices/clone            Clone a voice from audio sample
GET    /api/v1/voices/                 List voice profiles
GET    /api/v1/voices/{id}             Get voice profile
DELETE /api/v1/voices/{id}             Delete voice profile
```

### Sessions & Messages

```
POST   /api/v1/sessions/create         Create conversation session
GET    /api/v1/sessions/{id}           Get session
POST   /api/v1/messages/send           Send message (REST fallback)
GET    /api/v1/messages/session/{id}   Get message history
```

### WebSocket

```
WS  /ws/session/{session_id}
```

**Client → Server messages:**

```json
{ "type": "text",      "text": "Hello!" }
{ "type": "audio",     "audio": "<base64-webm>" }
{ "type": "set_voice", "voice_wav_path": "/abs/path/to/voice.wav" }
{ "type": "ping" }
```

**Server → Client messages:**

```json
{ "type": "transcription", "text": "..." }
{ "type": "message",       "role": "assistant", "content": "..." }
{ "type": "video",         "video_url": "http://localhost:8000/uploads/videos/..." }
{ "type": "status",        "message": "Thinking…", "stage": "llm" }
{ "type": "error",         "message": "..." }
{ "type": "pong" }
```

---

## Configuration

Key `.env` settings:

```bash
# LLM
LLM_PROVIDER=anthropic          # anthropic | openai | ollama
LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Avatar animation engine
AVATAR_ENGINE=sadtalker         # sadtalker | liveportrait | simple

# TTS
TTS_PROVIDER=coqui              # coqui | elevenlabs | bark
ELEVENLABS_API_KEY=...          # optional

# STT
WHISPER_MODEL=base              # tiny | base | small | medium | large

# Storage (local by default — no AWS needed)
USE_LOCAL_STORAGE=true          # set false to use AWS S3
LOCAL_STORAGE_PATH=uploads      # directory for local file storage
AWS_ACCESS_KEY_ID=...           # only needed when USE_LOCAL_STORAGE=false
AWS_SECRET_ACCESS_KEY=...
S3_BUCKET_NAME=...

# URLs
BACKEND_URL=http://localhost:8000
FRONTEND_URL=http://localhost:3000
```

---

## Deployment

### Self-hosted (any OS with Docker)

```bash
docker compose -f docker-compose.prod.yml up -d
```

### AWS (Terraform + ECS Fargate)

```bash
cd infrastructure
terraform init
terraform apply -var="environment=production"
./deploy.sh production
```

For production with S3, set `USE_LOCAL_STORAGE=false` and provide AWS credentials in `.env`.

---

## Tech Stack

**Frontend:** Next.js 16 · React 18 · TypeScript · Tailwind CSS · React Query · WebSocket · Canvas API (waveform)

**Backend:** FastAPI · SQLAlchemy (async) · PostgreSQL · Redis · Celery

**AI/ML:** Claude / GPT-4 / Llama 3 · Whisper (faster-whisper) · XTTS v2 (Coqui TTS) · SadTalker · Live Portrait

**Infrastructure:** Docker · Nginx · Local FS or AWS S3 + CloudFront · Terraform · Prometheus

---

## Contributing

PRs welcome! Please open an issue first for larger changes.

```bash
git checkout -b feature/my-feature
git commit -m "feat: add my feature"
git push origin feature/my-feature
# open pull request
```

---

## License

MIT © 2025

---

<div align="center">
  <sub>Built with FastAPI + Next.js + SadTalker</sub>
</div>
