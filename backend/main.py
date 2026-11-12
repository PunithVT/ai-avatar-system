import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import select, text

from app.api.v1 import avatars, conversations, messages, sessions, users, voices
from app.config import settings
from app.database import AsyncSessionLocal, Base, engine
from app.logging_config import configure_logging
from app.middleware.rate_limiter import RateLimitMiddleware
from app.middleware.security import RequestLoggingMiddleware, SecurityHeadersMiddleware
from app.models import Session as SessionModel
from app.services.cache import cache_service
from app.services.storage import storage_service
from app.services.turn_taking import turn_detection_mode, turn_taking_installed
from app.services.webrtc import WEBRTC_AVAILABLE, media_descriptor, mic_enabled, webrtc_enabled
from app.telemetry import init_telemetry
from app.tokens import subject_from_token
from app.websocket import websocket_manager

# Configure logging FIRST — every import below may log on module load.
configure_logging()
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

    # Schema management:
    #   * Production runs `alembic upgrade head` in entrypoint.sh BEFORE the
    #     app starts, so the schema (including the perf indexes from
    #     migration 0002) is already current here. We must NOT run
    #     create_all in that path — create_all silently ignores index
    #     additions on existing tables, so relying on it would hide
    #     migration drift and leave prod without the indexes.
    #   * Local dev / quick starts without the entrypoint get create_all as a
    #     convenience so `uvicorn main:app` against a fresh DB just works.
    # Gate on DEBUG to pick the right behavior.
    if settings.DEBUG:
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables created/verified (DEBUG create_all)")
        except Exception as e:
            logger.warning(f"Database not available at startup (will retry on first request): {e}")
    else:
        logger.info("Skipping create_all; schema is managed by Alembic (alembic upgrade head)")

    # Initialize services (non-fatal)
    try:
        await storage_service.initialize()
    except Exception as e:
        logger.warning(f"Storage service init failed: {e}")
    try:
        await cache_service.initialize()
    except Exception as e:
        logger.warning(f"Cache service init failed: {e}")

    # Mount local uploads directory so the browser can fetch images/videos
    if getattr(settings, "USE_LOCAL_STORAGE", True):
        uploads_dir = Path(settings.LOCAL_STORAGE_PATH)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")
        logger.info(f"Serving local uploads from {uploads_dir}")

    websocket_manager.start_cleanup_task()

    # The ready frame says whether the server will take the microphone, which
    # depends on Pipecat importing. Find out here, off the loop, rather than
    # inside the first connection's handshake. Only on the WebRTC transport:
    # on clips nothing ever asks.
    if webrtc_enabled():
        try:
            await asyncio.to_thread(mic_enabled)
        except Exception as e:
            logger.warning(f"Turn-detection check failed: {e}")

    # Load models now rather than on the first turn. In the background: the
    # server should accept connections (and pass its health check) while the
    # weights load, not sit unreachable for a minute.
    warmup_task = None
    if settings.PRELOAD_MODELS:
        warmup_task = asyncio.create_task(_warm_models(), name="model-warmup")

    logger.info("AI Avatar System started successfully")

    yield

    # Cleanup
    logger.info("Shutting down AI Avatar System...")
    if warmup_task is not None and not warmup_task.done():
        warmup_task.cancel()
        await asyncio.gather(warmup_task, return_exceptions=True)
    await websocket_manager.stop_cleanup_task()
    # Peer connections hold UDP sockets and sender tasks of their own; they
    # do not go away just because the WebSocket that negotiated them did.
    await websocket_manager.close_all_streams()
    # Without this the MuseTalk worker process outlived the server on a
    # reload or restart, still holding its VRAM.
    try:
        from app.services.animator import avatar_animator

        await avatar_animator.shutdown()
    except Exception as e:
        logger.warning(f"Animator shutdown failed: {e}")
    await storage_service.cleanup()
    await cache_service.cleanup()
    logger.info("Shutdown complete")


async def _warm_models() -> None:
    """
    Load STT, TTS, the lip-sync worker and — where the microphone comes over
    WebRTC — the turn-detection models, concurrently. Never raises.
    """
    from app.services import turn_taking
    from app.services.animator import avatar_animator
    from app.services.stt import stt_service
    from app.services.tts import tts_service

    logger.info("Preloading models (PRELOAD_MODELS=true)…")
    warmups = [stt_service.warm(), tts_service.warm(), avatar_animator.warm()]
    if mic_enabled():
        warmups.append(turn_taking.warm())
    await asyncio.gather(*warmups, return_exceptions=True)
    logger.info("Model preload finished")


app = FastAPI(
    title="AI Avatar System API",
    description="Real-time AI Avatar conversation system with lip-sync animation and voice cloning",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    # The schema too, not just the pages that render it: with only the UIs
    # disabled, /openapi.json still handed the full API map to anyone.
    openapi_url="/openapi.json" if settings.DEBUG else None,
)

# Middleware. `add_middleware` INSERTS AT THE FRONT of the stack, so the
# LAST one registered here is the OUTERMOST layer at request time. The
# registrations below therefore read inside-out: GZip is innermost,
# SecurityHeaders outermost.
#
# The ordering is load-bearing:
#   * SecurityHeaders must be outermost so the hardening headers are applied
#     to EVERY response — including a 429 short-circuited by the rate
#     limiter and CORS preflight replies, which never reach inner layers.
#   * RequestLogging sits above the rate limiter so rejected requests still
#     produce a log line; below it, 429s were dropped silently and rate-limit
#     abuse was invisible in the logs.
#   * CORS also sits above the rate limiter, so a 429 carries the
#     Access-Control-Allow-Origin header. Without that a browser reports an
#     opaque CORS failure instead of surfacing the actual 429 and its
#     Retry-After to the user.
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# Prometheus metrics
if settings.PROMETHEUS_ENABLED:
    Instrumentator().instrument(app).expose(app)

# Distributed tracing (no-op unless OTEL_ENABLED + packages installed)
init_telemetry(app)

# Routers
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
app.include_router(avatars.router, prefix="/api/v1/avatars", tags=["avatars"])
app.include_router(sessions.router, prefix="/api/v1/sessions", tags=["sessions"])
app.include_router(conversations.router, prefix="/api/v1/conversations", tags=["conversations"])
app.include_router(messages.router, prefix="/api/v1/messages", tags=["messages"])
app.include_router(voices.router, prefix="/api/v1/voices", tags=["voices"])


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
async def health_check(response: Response):
    """
    Liveness/readiness probe with three states:

      healthy   — everything reachable                        -> 200
      degraded  — a soft/configuration problem, still serving -> 200
      unhealthy — a hard dependency is down                   -> 503

    Only Postgres and Redis are hard dependencies. This used to always reply
    200, even with `{"status": "degraded"}` in the body, so Docker
    healthchecks, Kubernetes probes and load-balancer target groups — all of
    which read the status code, not the payload — kept routing traffic to an
    instance that could not reach its database.

    A missing LLM API key is deliberately NOT a 503: it is a config error the
    operator needs to see, but taking every replica out of rotation for it
    would turn a misconfiguration into a full outage.
    """
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
        health["status"] = "unhealthy"  # hard dependency

    # Check Redis
    try:
        if cache_service.redis:
            await cache_service.redis.ping()
            services["redis"] = "connected"
        else:
            services["redis"] = "not configured"
    except Exception:
        services["redis"] = "disconnected"
        health["status"] = "unhealthy"  # hard dependency

    # GPU / avatar engine info
    try:
        import torch

        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            used_gb = torch.cuda.memory_allocated(0) / 1024**3
            total_gb = props.total_memory / 1024**3
            services["gpu"] = f"{props.name} ({used_gb:.1f}/{total_gb:.1f} GB used)"
        else:
            services["gpu"] = "not available (CPU mode)"
    except ImportError:
        services["gpu"] = "torch not installed"
    except Exception:
        services["gpu"] = "error"

    # LLM provider readiness — checks client wiring + API-key presence, not
    # a live network call (which would cost tokens on every /health hit).
    if settings.LLM_PROVIDER == "anthropic":
        services["llm"] = (
            "ready (anthropic)" if settings.ANTHROPIC_API_KEY else "missing ANTHROPIC_API_KEY"
        )
        if not settings.ANTHROPIC_API_KEY and health["status"] == "healthy":
            health["status"] = "degraded"  # misconfigured, not down
    elif settings.LLM_PROVIDER == "openai":
        services["llm"] = "ready (openai)" if settings.OPENAI_API_KEY else "missing OPENAI_API_KEY"
        if not settings.OPENAI_API_KEY and health["status"] == "healthy":
            health["status"] = "degraded"  # misconfigured, not down
    elif settings.LLM_PROVIDER == "ollama":
        # Local OpenAI-compatible server — no API key needed.
        services["llm"] = (
            f"ready (ollama @ {settings.OPENAI_BASE_URL or 'http://localhost:11434/v1'})"
        )
    else:
        services["llm"] = f"unknown provider: {settings.LLM_PROVIDER}"
        if health["status"] == "healthy":
            health["status"] = "degraded"  # misconfigured, not down

    # STT / TTS model state — lazy-loaded, so just report whether warmed
    try:
        from app.services.stt import stt_service
        from app.services.tts import tts_service

        services["stt"] = "loaded" if stt_service.model is not None else "lazy (not yet loaded)"
        services["tts"] = "loaded" if tts_service.model is not None else "lazy (not yet loaded)"
    except Exception as e:
        services["stt"] = services["tts"] = f"error: {e}"

    health["avatar_engine"] = settings.AVATAR_ENGINE
    # What is ACTUALLY animating, not just what was configured: a worker that
    # failed to start means every reply is a still image, and nothing else
    # surfaced that.
    try:
        from app.services.animator import avatar_animator

        health["avatar_engine_state"] = avatar_animator.worker_state()
    except Exception as e:
        health["avatar_engine_state"] = f"error: {e}"
    health["active_ws_sessions"] = len(websocket_manager.active_connections)
    # Configured vs. possible: MEDIA_TRANSPORT=webrtc on an image without
    # aiortc quietly serves clips, and this is where that shows.
    health["media_transport"] = settings.MEDIA_TRANSPORT
    health["webrtc_available"] = WEBRTC_AVAILABLE
    # Likewise for who takes the turns in hands-free: "smart" configured
    # without pipecat installed quietly leaves it to the browser. (Only
    # matters on the webrtc transport; reported regardless.)
    health["voice_turn_detection"] = turn_detection_mode()
    health["voice_turn_detection_available"] = turn_taking_installed()

    # Only a hard-dependency failure takes the instance out of rotation.
    if health["status"] == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return health


# WebSocket close codes for a refused handshake. They are distinct because the
# client must react differently to each, and conflating them is destructive:
# when "this conversation was deleted" and "the database hiccuped" both looked
# like "your login expired", the client signed the user out — and a guest,
# whose token is their only way back into the account, lost it for good.
WS_CLOSE_UNAUTHENTICATED = 4401  # no token, or a token that does not verify
WS_CLOSE_SESSION_UNAVAILABLE = 4404  # no such session, or not the caller's
WS_CLOSE_INTERNAL_ERROR = 1011  # we failed to check; safe to retry


async def _ws_handshake(session_id: str, token: str | None) -> tuple[str | None, int]:
    """
    Decide a WebSocket handshake. Returns (user_id, 0) to accept, or
    (None, close_code) to refuse.

    A token is always required. There used to be a DEBUG fallback to a seeded
    `demo-user`, but sessions can no longer be created anonymously, so nothing
    can resolve to that identity.
    """
    user_id = subject_from_token(token) if token else None
    if not user_id:
        return None, WS_CLOSE_UNAUTHENTICATED
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
            sess = result.scalar_one_or_none()
    except Exception as e:
        logger.error(f"WS session verification failed: {e}")
        return None, WS_CLOSE_INTERNAL_ERROR
    # Unknown and not-yours are deliberately the same answer: telling them
    # apart would confirm to a stranger that a session id exists.
    if sess is None or sess.user_id != user_id:
        return None, WS_CLOSE_SESSION_UNAVAILABLE
    return user_id, 0


async def _verify_ws_session(session_id: str, token: str | None) -> str | None:
    """The owning user_id if the handshake is acceptable, else None."""
    user_id, _ = await _ws_handshake(session_id, token)
    return user_id


@app.websocket("/ws/session/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    session_id: str,
    token: str | None = Query(default=None),
):
    # Prefer the explicit ?token= (cross-origin clients); fall back to the
    # httpOnly auth cookie the browser sends automatically on same-site WS.
    if token is None:
        token = websocket.cookies.get(settings.AUTH_COOKIE_NAME)
    user_id, refusal = await _ws_handshake(session_id, token)
    if user_id is None:
        # Accept first: closing a socket that was never accepted is sent as an
        # HTTP 403 on the handshake, which a browser reports as close code
        # 1006 ("abnormal") whatever we meant. The client could not tell an
        # expired login from a network fault, so it retried with backoff and
        # then reported a lost connection instead of asking the user to sign in.
        await websocket.accept()
        await websocket.close(code=refusal)
        logger.warning(f"WS handshake refused for session {session_id} (close {refusal})")
        return

    # Refuse new sockets once WS_MAX_CONNECTIONS is reached. A reconnect to a
    # session that is already open is allowed through — it replaces the
    # existing entry rather than consuming another slot. 1013 ("try again
    # later") is the standard code for a temporary capacity refusal.
    if session_id not in websocket_manager.active_connections and (
        await websocket_manager.at_capacity()
    ):
        await websocket.accept()  # so the code below reaches the client
        await websocket.close(code=1013)
        logger.warning(
            f"WS connection refused for session {session_id}: "
            f"at capacity ({settings.WS_MAX_CONNECTIONS})"
        )
        return

    await websocket_manager.connect(session_id, websocket, user_id=user_id)
    # First frame of every accepted connection. Refusals are also "opened"
    # (see above), so a client cannot take the socket opening as success;
    # this frame is what tells it the connection is real.
    # `media` tells it whether a WebRTC offer would be welcome (and with
    # which ICE servers); when it says no, nothing else about the session
    # differs from before the field existed. `media.mic` says whether the
    # server will also take the microphone over that connection and decide
    # the turns itself.
    await websocket_manager.send_message(session_id, {"type": "ready", "media": media_descriptor()})
    try:
        while True:
            # This loop must never block on a full turn — handle_text_input /
            # handle_audio_input dispatch the work as a background task and
            # return immediately, so we come straight back to receive_json()
            # and can observe a barge-in ("text"/"audio" mid-response) or an
            # explicit "stop". All sends route through the manager's locked
            # send_message so they can't interleave with the turn task's
            # streaming sends.
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "audio":
                audio_data = data.get("audio")
                if not audio_data:
                    await websocket_manager.send_message(
                        session_id, {"type": "error", "message": "Missing audio data"}
                    )
                    continue
                await websocket_manager.handle_audio_input(session_id, audio_data)

            elif msg_type == "text":
                text_data = data.get("text")
                if not text_data:
                    await websocket_manager.send_message(
                        session_id, {"type": "error", "message": "Missing text data"}
                    )
                    continue
                await websocket_manager.handle_text_input(session_id, text_data)

            elif msg_type in ("stop", "interrupt"):
                # Explicit barge-in from a "stop" button — cancel the current
                # turn without starting a new one.
                await websocket_manager.interrupt_active_turn(session_id)

            elif msg_type == "webrtc_offer":
                # Answers (or refuses) over this socket; never raises.
                await websocket_manager.handle_webrtc_offer(session_id, data.get("sdp"))

            elif msg_type == "webrtc_close":
                await websocket_manager.handle_webrtc_close(session_id)

            elif msg_type == "mic":
                # The stream microphone on (after the client attached its
                # track) or off. Answered with mic_state; never raises.
                await websocket_manager.handle_mic(session_id, data.get("active"))

            elif msg_type == "set_voice":
                # Accept voice_id only — never a raw filesystem path from the client.
                voice_id = data.get("voice_id")
                if not voice_id or not isinstance(voice_id, str):
                    await websocket_manager.send_message(
                        session_id, {"type": "error", "message": "Missing voice_id"}
                    )
                    continue
                ok = await websocket_manager.set_voice_by_id(session_id, voice_id)
                if not ok:
                    await websocket_manager.send_message(
                        session_id, {"type": "error", "message": "Voice profile not found"}
                    )

            elif msg_type == "set_language":
                lang = data.get("language", "en")
                await websocket_manager.set_language(session_id, lang)

            elif msg_type == "ping":
                await websocket_manager.send_message(session_id, {"type": "pong"})

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from session {session_id}")
        # Scoped to THIS socket: if a newer connection has taken the session
        # over, its state is not ours to tear down.
        await websocket_manager.disconnect(session_id, websocket)
    except Exception as e:
        logger.error(f"WebSocket error in session {session_id}: {e}")
        await websocket_manager.disconnect(session_id, websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
