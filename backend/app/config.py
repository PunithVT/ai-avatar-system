from pathlib import Path
from typing import List, Optional, Union

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# Resolve .env path relative to this file's location (always project root)
_ENV_FILE = str(Path(__file__).resolve().parent.parent.parent / ".env")

_WEAK_SECRETS = {"change-this-secret-key", "change-this-jwt-secret", "change-this-jwt-secret-key"}


class Settings(BaseSettings):
    # Application
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str

    # Database
    DATABASE_URL: str = "postgresql://avatar_user:password@localhost:5432/avatar_db"
    DATABASE_NAME: str = "avatar_db"
    DATABASE_USER: str = "avatar_user"
    DATABASE_PASSWORD: str = "password"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Storage — local by default; set USE_LOCAL_STORAGE=false to use S3
    USE_LOCAL_STORAGE: bool = True
    LOCAL_STORAGE_PATH: str = "uploads"

    # AWS (only needed when USE_LOCAL_STORAGE=false)
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = "avatar-system-storage"
    CLOUDFRONT_DOMAIN: Optional[str] = None

    # API Keys
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""

    # Point the OpenAI-compatible client at a different server — Ollama
    # (http://localhost:11434/v1), vLLM, LM Studio, OpenRouter, etc.
    # Used when LLM_PROVIDER is "openai" or "ollama".
    OPENAI_BASE_URL: Optional[str] = None

    # LLM Configuration
    # Anthropic models (2026): claude-opus-4-7 (most capable), claude-sonnet-4-6
    # (balanced — current default), claude-haiku-4-5 (fastest). OpenAI users
    # should override LLM_MODEL via .env (e.g. gpt-4o, gpt-4o-mini).
    # "ollama" runs fully local & free: set LLM_MODEL to e.g. llama3.1 and
    # optionally OPENAI_BASE_URL (defaults to http://localhost:11434/v1).
    LLM_PROVIDER: str = "anthropic"  # anthropic, openai, ollama
    LLM_MODEL: str = "claude-sonnet-4-6"
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 2000
    # For OpenAI-compatible reasoning ("thinking") models — notably Ollama
    # cloud models like glm-*:cloud / kimi-*:cloud — set to "none" to disable
    # the reasoning phase. Without it those models emit their text as
    # `reasoning` and leave `content` empty, so the real-time pipeline gets
    # nothing. Forwarded verbatim in the request body; leave empty for normal
    # (non-reasoning) models so nothing is sent.
    LLM_REASONING_EFFORT: str = ""

    # Avatar Engine
    AVATAR_ENGINE: str = "musetalk"  # musetalk, simple
    AVATAR_RESOLUTION: int = 512
    AVATAR_FPS: int = 25
    MUSETALK_PATH: str = "models/MuseTalk"

    # STT Configuration
    # large-v3-turbo: best 2026 sweet spot — ~216x real-time on GPU, multilingual,
    # only ~1% lower WER than large-v3. Falls back to base/small if VRAM is tight.
    STT_PROVIDER: str = "whisper"  # whisper, google, azure
    WHISPER_MODEL: str = "large-v3-turbo"  # tiny, base, small, medium, large-v3, large-v3-turbo

    # TTS Configuration
    # chatterbox: Resemble AI's open-source SOTA TTS (default, voice cloning + 23 langs)
    TTS_PROVIDER: str = "chatterbox"

    # Security
    # Union[..., str] lets pydantic-settings keep a non-JSON env value as a
    # raw string instead of failing the JSON parse, so both formats work:
    #   CORS_ORIGINS=["http://a","http://b"]   (JSON)
    #   CORS_ORIGINS=http://a,http://b         (comma-separated)
    CORS_ORIGINS: Union[List[str], str] = ["http://localhost:3000", "http://localhost:8000"]
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 24

    # Auth cookie. Login sets the JWT in an httpOnly cookie (not readable by
    # JS → not stealable via XSS). `Secure` should be true in production
    # (HTTPS only); SameSite=lax works across same-site ports (localhost:3000
    # → :8000 in dev) and blocks cross-site POST, which covers most CSRF.
    AUTH_COOKIE_NAME: str = "access_token"
    AUTH_COOKIE_SECURE: bool = False  # set true in production (.env.prod)
    AUTH_COOKIE_SAMESITE: str = "lax"  # lax | strict | none
    AUTH_COOKIE_DOMAIN: Optional[str] = None

    # Guest accounts. "Continue as Guest" mints a real (anonymous) user row
    # so the ordinary per-user ownership checks scope a guest's data to just
    # that guest. They are disposable: the daily cleanup task deletes guest
    # users whose sessions have all been idle longer than this.
    GUEST_ACCOUNTS_ENABLED: bool = True
    GUEST_RETENTION_HOURS: int = 48

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

    # File Upload. Enforced while streaming the request body (see
    # app.api.v1.avatars._read_capped) so an oversized upload is rejected
    # without ever being buffered in full.
    MAX_UPLOAD_SIZE: int = 10485760  # 10MB
    ALLOWED_EXTENSIONS: Union[List[str], str] = ["jpg", "jpeg", "png", "webp"]
    # Upper bound on the free-text avatar name accepted at upload time —
    # matches AvatarRename.name so both entry points agree.
    MAX_AVATAR_NAME_LEN: int = 200

    # Monitoring
    SENTRY_DSN: Optional[str] = None
    PROMETHEUS_ENABLED: bool = True

    # Distributed tracing (OpenTelemetry). Off by default — when enabled,
    # requires the optional `opentelemetry-*` packages (see
    # requirements-otel.txt). Spans are no-ops when disabled, so the
    # instrumentation in the hot path costs nothing in the default build.
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "avatar-backend"
    # OTLP/gRPC collector endpoint, e.g. "http://otel-collector:4317".
    OTEL_EXPORTER_OTLP_ENDPOINT: Optional[str] = None

    # URLs
    FRONTEND_URL: str = "http://localhost:3000"
    BACKEND_URL: str = "http://localhost:8000"

    @field_validator("CORS_ORIGINS", "ALLOWED_EXTENSIONS", mode="before")
    @classmethod
    def _split_comma_separated(cls, value):
        """Accept comma-separated env strings (.env.example style) as lists."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _validate_production_hardening(self) -> "Settings":
        """
        Fail fast when a production deployment is configured unsafely.

        DEBUG is the single switch that unlocks several dev-only behaviours:
        interactive API docs, `create_all` instead of Alembic, raw exception
        text in 5xx bodies, and SQL echo (which logs statement parameters).
        A deploy that sets ENVIRONMENT=production but leaves DEBUG at its
        `True` default silently gets all of them, so refuse to start rather
        than run a production service in that state.
        """
        if self.ENVIRONMENT != "production":
            return self

        problems: list[str] = []
        if self.DEBUG:
            problems.append("DEBUG must be false in production")
        if not self.AUTH_COOKIE_SECURE:
            problems.append(
                "AUTH_COOKIE_SECURE must be true in production so the auth "
                "cookie is never sent over plain HTTP"
            )
        if "*" in self.CORS_ORIGINS:
            problems.append(
                "CORS_ORIGINS must not contain '*' in production — list your exact origins instead"
            )
        if any(str(o).startswith("http://") for o in self.CORS_ORIGINS):
            problems.append("CORS_ORIGINS must use https:// in production")
        if problems:
            raise ValueError("Unsafe production configuration:\n  - " + "\n  - ".join(problems))
        return self

    @model_validator(mode="after")
    def _validate_secrets(self) -> "Settings":
        for field, value in (
            ("SECRET_KEY", self.SECRET_KEY),
            ("JWT_SECRET_KEY", self.JWT_SECRET_KEY),
        ):
            if value in _WEAK_SECRETS:
                raise ValueError(
                    f"{field} is set to an insecure default — set a strong random value in .env"
                )
            if len(value) < 32:
                raise ValueError(f"{field} must be at least 32 characters")
        return self

    model_config = {
        "env_file": _ENV_FILE,
        "case_sensitive": True,
        # Ignore (rather than reject) unrecognised keys. Operators carry
        # long-lived .env files; a setting retired from this class must not
        # turn their existing file into a startup crash.
        "extra": "ignore",
    }


settings = Settings()
