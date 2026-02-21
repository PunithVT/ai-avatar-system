from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
import logging
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import settings
from app.database import engine, Base
from app.api.v1 import avatars, conversations, messages, sessions, users
from app.api.v1 import voices
from app.websocket import websocket_manager
from app.services.storage import storage_service

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AI Avatar System...")

    # Create database tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Initialise storage (creates uploads/ dir in local mode)
    await storage_service.initialize()

    # Mount local uploads directory so the browser can fetch images/videos
    if getattr(settings, "USE_LOCAL_STORAGE", True):
        uploads_dir = Path(settings.LOCAL_STORAGE_PATH)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")
        logger.info(f"Serving local uploads from {uploads_dir}")

    logger.info("AI Avatar System started successfully")
    yield

    logger.info("Shutting down AI Avatar System...")
    await storage_service.cleanup()
    logger.info("Shutdown complete")


app = FastAPI(
    title="AI Avatar System API",
    description="Real-time AI Avatar conversation system with lip-sync animation and voice cloning",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

if settings.PROMETHEUS_ENABLED:
    Instrumentator().instrument(app).expose(app)

# Routers
app.include_router(users.router,         prefix="/api/v1/users",         tags=["users"])
app.include_router(avatars.router,       prefix="/api/v1/avatars",       tags=["avatars"])
app.include_router(sessions.router,      prefix="/api/v1/sessions",      tags=["sessions"])
app.include_router(conversations.router, prefix="/api/v1/conversations",  tags=["conversations"])
app.include_router(messages.router,      prefix="/api/v1/messages",      tags=["messages"])
app.include_router(voices.router,        prefix="/api/v1/voices",        tags=["voices"])


@app.get("/")
async def root():
    return {"name": "AI Avatar System API", "version": "2.0.0", "status": "running"}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "environment": settings.ENVIRONMENT}


@app.websocket("/ws/session/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket_manager.connect(session_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "audio":
                await websocket_manager.handle_audio_input(session_id, data.get("audio"))

            elif msg_type == "text":
                await websocket_manager.handle_text_input(session_id, data.get("text"))

            elif msg_type == "set_voice":
                # Client sends chosen voice WAV path (returned by /api/v1/voices/clone)
                voice_wav = data.get("voice_wav_path")
                if voice_wav:
                    await websocket_manager.set_voice(session_id, voice_wav)

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from session {session_id}")
        await websocket_manager.disconnect(session_id)
    except Exception as e:
        logger.error(f"WebSocket error in session {session_id}: {e}")
        await websocket_manager.disconnect(session_id)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred" if not settings.DEBUG else str(exc)},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000,
                reload=settings.DEBUG, log_level=settings.LOG_LEVEL.lower())
