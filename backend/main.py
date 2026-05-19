from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import text, select
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
import logging
from datetime import datetime, timezone

from jose import JWTError, jwt
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import settings
from app.database import engine, Base, AsyncSessionLocal
from app.models import User, Session as SessionModel
from app.api.v1 import avatars, conversations, messages, sessions, users
from app.api.v1 import voices
from app.websocket import websocket_manager
from app.services.storage import storage_service
from app.services.cache import cache_service
from app.middleware.rate_limiter import RateLimitMiddleware
from app.middleware.security import SecurityHeadersMiddleware, RequestLoggingMiddleware

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Sentry if DSN is configured
if settings.SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            traces_sample_rate=0.1 if settings.ENVIRONMENT == "production" else 1.0,
            environment=settings.ENVIRONMENT,
            release="avatar-system@2.0.0",
        )
        logger.info("Sentry initialized successfully")
    except Exception as e:
        logger.warning(f"Failed to initialize Sentry: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AI Avatar System...")

    # Create database tables (non-fatal if DB not available yet)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created/verified")
    except Exception as e:
        logger.warning(f"Database not available at startup (will retry on first request): {e}")

    # Initialize services (non-fatal)
    try:
        await storage_service.initialize()
    except Exception as e:
        logger.warning(f"Storage service init failed: {e}")
    try:
        await cache_service.initialize()
    except Exception as e:
        logger.warning(f"Cache service init failed: {e}")

    # Seed demo user ONLY in DEBUG/development mode. An empty-password user
    # in production would be a critical auth bypass.
    if settings.DEBUG:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(User).where(User.id == "demo-user"))
                if result.scalar_one_or_none() is None:
                    session.add(User(
                        id="demo-user",
                        email="demo@localhost",
                        username="demo",
                        hashed_password="",  # disabled — login route rejects empty passwords
                        full_name="Demo User",
                    ))
                    await session.commit()
                    logger.info("Demo user created (DEBUG mode)")
        except Exception as e:
            logger.warning(f"Could not seed demo user: {e}")

    # Mount local uploads directory so the browser can fetch images/videos
    if getattr(settings, "USE_LOCAL_STORAGE", True):
        uploads_dir = Path(settings.LOCAL_STORAGE_PATH)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")
        logger.info(f"Serving local uploads from {uploads_dir}")

    logger.info("AI Avatar System started successfully")

    yield

    # Cleanup
    logger.info("Shutting down AI Avatar System...")
    await storage_service.cleanup()
    await cache_service.cleanup()
    logger.info("Shutdown complete")


app = FastAPI(
    title="AI Avatar System API",
    description="Real-time AI Avatar conversation system with lip-sync animation and voice cloning",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# Middleware (order matters — outermost first)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Prometheus metrics
if settings.PROMETHEUS_ENABLED:
    Instrumentator().instrument(app).expose(app)

# Routers
app.include_router(users.router,         prefix="/api/v1/users",         tags=["users"])
app.include_router(avatars.router,       prefix="/api/v1/avatars",       tags=["avatars"])
app.include_router(sessions.router,      prefix="/api/v1/sessions",      tags=["sessions"])
app.include_router(conversations.router, prefix="/api/v1/conversations",  tags=["conversations"])
app.include_router(messages.router,      prefix="/api/v1/messages",      tags=["messages"])
app.include_router(voices.router,        prefix="/api/v1/voices",        tags=["voices"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.url.path}: {exc}", exc_info=True)
    content: dict = {"detail": "Internal server error"}
    if settings.DEBUG:
        # Only surface the raw exception text in development — in production
        # it can leak API keys, DB DSNs, file paths, etc.
        content["error"] = str(exc)
    return JSONResponse(status_code=500, content=content)


@app.get("/")
async def root():
    return {
        "name": "AI Avatar System API",
        "version": "2.0.0",
        "status": "running",
        "environment": settings.ENVIRONMENT,
    }


@app.get("/health")
async def health_check():
    services: dict[str, str] = {}
    health: dict[str, object] = {
        "status": "healthy",
        "environment": settings.ENVIRONMENT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": services,
    }

    # Check database
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        services["database"] = "connected"
    except Exception:
        services["database"] = "disconnected"
        health["status"] = "degraded"

    # Check Redis
    try:
        if cache_service.redis:
            await cache_service.redis.ping()
            services["redis"] = "connected"
        else:
            services["redis"] = "not configured"
    except Exception:
        services["redis"] = "disconnected"
        health["status"] = "degraded"

    # GPU / avatar engine info
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            used_gb = torch.cuda.memory_allocated(0) / 1024 ** 3
            total_gb = props.total_memory / 1024 ** 3
            services["gpu"] = f"{props.name} ({used_gb:.1f}/{total_gb:.1f} GB used)"
        else:
            services["gpu"] = "not available (CPU mode)"
    except ImportError:
        services["gpu"] = "torch not installed"
    except Exception:
        services["gpu"] = "error"

    health["avatar_engine"] = settings.AVATAR_ENGINE

    return health


async def _verify_ws_session(session_id: str, token: str | None) -> str | None:
    """
    Validate the WebSocket handshake. Returns the user_id that owns the session
    or None if the session is unknown / token invalid / token user doesn't own
    the session.

    In DEBUG mode (single-user dev) we fall back to the seeded `demo-user`
    when no token is supplied.
    """
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SessionModel).where(SessionModel.id == session_id)
            )
            sess = result.scalar_one_or_none()
            if not sess:
                return None

            if token:
                try:
                    payload = jwt.decode(
                        token,
                        settings.JWT_SECRET_KEY,
                        algorithms=[settings.JWT_ALGORITHM],
                    )
                    user_id = payload.get("sub")
                except JWTError:
                    return None
                if user_id and user_id == sess.user_id:
                    return user_id
                return None

            # No token: only allowed for the demo session in DEBUG mode
            if settings.DEBUG and sess.user_id == "demo-user":
                return "demo-user"
            return None
    except Exception as e:
        logger.error(f"WS session verification failed: {e}")
        return None


@app.websocket("/ws/session/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: str,
    token: str | None = Query(default=None),
):
    user_id = await _verify_ws_session(session_id, token)
    if user_id is None:
        # 4401 is a custom WebSocket close code we use for auth failures
        await websocket.close(code=4401)
        logger.warning(f"WS auth rejected for session {session_id}")
        return

    await websocket_manager.connect(session_id, websocket, user_id=user_id)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "audio":
                audio_data = data.get("audio")
                if not audio_data:
                    await websocket.send_json({"type": "error", "message": "Missing audio data"})
                    continue
                await websocket_manager.handle_audio_input(session_id, audio_data)

            elif msg_type == "text":
                text_data = data.get("text")
                if not text_data:
                    await websocket.send_json({"type": "error", "message": "Missing text data"})
                    continue
                await websocket_manager.handle_text_input(session_id, text_data)

            elif msg_type == "set_voice":
                # Accept voice_id only — never a raw filesystem path from the client.
                voice_id = data.get("voice_id")
                if not voice_id or not isinstance(voice_id, str):
                    await websocket.send_json({"type": "error", "message": "Missing voice_id"})
                    continue
                ok = await websocket_manager.set_voice_by_id(session_id, voice_id)
                if not ok:
                    await websocket.send_json({"type": "error", "message": "Voice profile not found"})

            elif msg_type == "set_language":
                lang = data.get("language", "en")
                await websocket_manager.set_language(session_id, lang)

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from session {session_id}")
        await websocket_manager.disconnect(session_id)
    except Exception as e:
        logger.error(f"WebSocket error in session {session_id}: {e}")
        await websocket_manager.disconnect(session_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000,
                reload=settings.DEBUG, log_level=settings.LOG_LEVEL.lower())
