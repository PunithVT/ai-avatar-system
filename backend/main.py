from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import settings
from app.database import engine, Base
from app.api.v1 import avatars, conversations, messages, sessions, users
from app.websocket import websocket_manager
from app.services.storage import storage_service

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    logger.info("Starting AI Avatar System...")
    
    # Create database tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Initialize storage
    await storage_service.initialize()
    
    logger.info("AI Avatar System started successfully")
    
    yield
    
    # Cleanup
    logger.info("Shutting down AI Avatar System...")
    await storage_service.cleanup()
    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="AI Avatar System API",
    description="Real-time AI Avatar conversation system with lip-sync animation",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add GZip compression
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Add Prometheus metrics
if settings.PROMETHEUS_ENABLED:
    Instrumentator().instrument(app).expose(app)


# Include routers
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
app.include_router(avatars.router, prefix="/api/v1/avatars", tags=["avatars"])
app.include_router(sessions.router, prefix="/api/v1/sessions", tags=["sessions"])
app.include_router(conversations.router, prefix="/api/v1/conversations", tags=["conversations"])
app.include_router(messages.router, prefix="/api/v1/messages", tags=["messages"])


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "AI Avatar System API",
        "version": "1.0.0",
        "status": "running",
        "environment": settings.ENVIRONMENT
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "environment": settings.ENVIRONMENT
    }


@app.websocket("/ws/session/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for real-time avatar streaming"""
    await websocket_manager.connect(session_id, websocket)
    
    try:
        while True:
            # Receive message from client
            data = await websocket.receive_json()
            
            # Handle different message types
            if data.get("type") == "audio":
                # Process audio input
                await websocket_manager.handle_audio_input(session_id, data.get("audio"))
            
            elif data.get("type") == "text":
                # Process text input
                await websocket_manager.handle_text_input(session_id, data.get("text"))
            
            elif data.get("type") == "ping":
                # Respond to ping
                await websocket.send_json({"type": "pong"})
    
    except WebSocketDisconnect:
        logger.info(f"Client disconnected from session {session_id}")
        await websocket_manager.disconnect(session_id)
    
    except Exception as e:
        logger.error(f"WebSocket error in session {session_id}: {e}")
        await websocket_manager.disconnect(session_id)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An internal server error occurred" if not settings.DEBUG else str(exc)
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower()
    )
