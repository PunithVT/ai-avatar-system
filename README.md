<div align="center">

# AvatarAI — Real-Time AI Avatar Conversation Platform

[![Next.js](https://img.shields.io/badge/Next.js-14-black?logo=next.js)](https://nextjs.org)
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

Inspired by projects like [Voicebox](https://github.com/jamiepine/voicebox), AvatarAI takes the local-first, privacy-conscious philosophy and applies it to interactive avatar experiences.

---

## Features

| Feature | Details |
|---|---|
| **LLM Backends** | Claude (Anthropic) · GPT-4 (OpenAI) · Llama 3 (Ollama, local) |
| **Voice Cloning** | Record 5–30 s → XTTS v2 speaker embedding |
| **Speech-to-Text** | Whisper (faster-whisper, CUDA) |
| **Lip-Sync Animation** | SadTalker · Live Portrait · Simple (fallback) |
| **Real-Time** | WebSocket pipeline — sub-200 ms to first token |
| **Emotion Detection** | Lightweight heuristic + server-side sentiment |
| **18+ Languages** | Whisper STT + XTTS v2 TTS |
| **Storage** | Local FS or AWS S3 + CloudFront CDN |
| **Privacy** | 100 % local mode — nothing leaves your machine |
| **Monitoring** | Prometheus metrics · Celery Flower · Sentry |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser / Client                          │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ Avatar Studio│  │  Voice Panel │  │    Chat Interface      │ │
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
                           Celery workers
```

### Data Flow (one conversation turn)
```
Mic/Text → [Whisper STT] → text → [Claude/GPT] → response →
[XTTS v2 TTS] → audio → [SadTalker] → lip-sync video →
WebSocket → browser video player
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

This starts:

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

---

## Voice Cloning

AvatarAI includes a full **Voice Studio** inspired by [Voicebox](https://github.com/jamiepine/voicebox):

1. Navigate to **Voice Studio** in the top nav
2. Click **Clone Voice**
3. Record 5–30 seconds of clear speech
4. Give it a name → **Clone This Voice**
5. The voice profile is saved and available for all future avatar conversations

Internally, the audio is stored as a WAV reference file and passed to **XTTS v2** as `speaker_wav` on every TTS call.

### Voice API

```bash
# Upload a voice sample
curl -X POST http://localhost:8000/api/v1/voices/clone \
  -F "audio=@sample.wav" \
  -F "name=My Voice" \
  -F "language=en"

# List voices
curl http://localhost:8000/api/v1/voices/

# Delete a voice
curl -X DELETE http://localhost:8000/api/v1/voices/{voice_id}
```

---

## API Reference

### Avatars
```
POST   /api/v1/avatars/upload          Upload avatar image
GET    /api/v1/avatars/                List all avatars
GET    /api/v1/avatars/{id}            Get avatar
DELETE /api/v1/avatars/{id}            Delete avatar
```

### Voice Cloning
```
POST   /api/v1/voices/clone            Clone a voice from audio sample
GET    /api/v1/voices/                 List voice profiles
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
{ "type": "text",  "text": "Hello!" }
{ "type": "audio", "audio": "<base64-webm>" }
{ "type": "ping" }
```

**Server → Client messages:**
```json
{ "type": "transcription", "text": "..." }
{ "type": "message",       "content": "..." }
{ "type": "video",         "video_url": "..." }
{ "type": "status",        "message": "..." }
{ "type": "error",         "message": "..." }
```

---

## Configuration

Key `.env` settings:

```bash
# LLM
LLM_PROVIDER=anthropic          # anthropic | openai | ollama
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Avatar animation engine
AVATAR_ENGINE=sadtalker         # sadtalker | live_portrait | simple

# TTS
TTS_PROVIDER=coqui              # coqui | elevenlabs | bark
ELEVENLABS_API_KEY=...          # optional

# STT
WHISPER_MODEL=base              # tiny | base | small | medium | large

# Storage
USE_S3=false                    # true to use AWS S3
AWS_BUCKET_NAME=...
```

---

## Deployment

### AWS (Terraform + ECS Fargate)

```bash
cd infrastructure
terraform init
terraform apply -var="environment=production"
./deploy.sh production
```

### Self-hosted

Any Linux server with Docker:
```bash
docker compose -f docker-compose.prod.yml up -d
```

---

## Tech Stack

**Frontend:** Next.js 14 · React 18 · TypeScript · Tailwind CSS · React Query · Zustand · socket.io

**Backend:** FastAPI · SQLAlchemy (async) · PostgreSQL · Redis · Celery

**AI/ML:** Claude / GPT-4 / Llama 3 · Whisper (faster-whisper) · XTTS v2 (Coqui TTS) · SadTalker · Live Portrait

**Infrastructure:** Docker · Nginx · Terraform · AWS ECS · S3 · CloudFront

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
  <sub>Inspired by <a href="https://github.com/jamiepine/voicebox">Voicebox</a> · Built with FastAPI + Next.js + SadTalker</sub>
</div>
