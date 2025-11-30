from pydantic_settings import BaseSettings
from typing import List, Optional
import os


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "AI Avatar System"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = "change-this-secret-key"
    
    # Database
    DATABASE_URL: str = "postgresql://avatar_user:password@localhost:5432/avatar_db"
    DATABASE_HOST: str = "localhost"
    DATABASE_PORT: int = 5432
    DATABASE_NAME: str = "avatar_db"
    DATABASE_USER: str = "avatar_user"
    DATABASE_PASSWORD: str = "password"
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    
    # AWS
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = "avatar-system-storage"
    CLOUDFRONT_DOMAIN: Optional[str] = None
    
    # API Keys
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ELEVENLABS_API_KEY: Optional[str] = None
    
    # LLM Configuration
    LLM_PROVIDER: str = "anthropic"  # anthropic, openai, ollama
    LLM_MODEL: str = "claude-sonnet-4-20250514"
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 2000
    
    # Avatar Engine
    AVATAR_ENGINE: str = "sadtalker"  # sadtalker, liveportrait
    AVATAR_RESOLUTION: int = 512
    AVATAR_FPS: int = 25
    
    # STT Configuration
    STT_PROVIDER: str = "whisper"  # whisper, google, azure
    WHISPER_MODEL: str = "base"  # tiny, base, small, medium, large
    
    # TTS Configuration
    TTS_PROVIDER: str = "coqui"  # coqui, elevenlabs, bark
    TTS_VOICE: str = "default"
    
    # Security
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8000"]
    JWT_SECRET_KEY: str = "change-this-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 24
    
    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 60
    RATE_LIMIT_PER_HOUR: int = 1000
    
    # WebSocket
    WS_MAX_CONNECTIONS: int = 1000
    WS_PING_INTERVAL: int = 30
    WS_PING_TIMEOUT: int = 10
    
    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    
    # File Upload
    MAX_UPLOAD_SIZE: int = 10485760  # 10MB
    ALLOWED_EXTENSIONS: List[str] = ["jpg", "jpeg", "png", "webp"]
    
    # Video Settings
    VIDEO_FPS: int = 25
    VIDEO_CODEC: str = "h264"
    VIDEO_BITRATE: str = "2000k"
    
    # Monitoring
    SENTRY_DSN: Optional[str] = None
    PROMETHEUS_ENABLED: bool = True
    
    # URLs
    FRONTEND_URL: str = "http://localhost:3000"
    BACKEND_URL: str = "http://localhost:8000"
    
    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
