"""
Per-identity rate limiter.

Identity key precedence:
  1. Authenticated JWT user → `user:<uuid>` (a botnet rotating IPs can't bypass)
  2. Client IP             → `ip:<addr>`     (fallback for anonymous traffic)

Storage backend precedence:
  1. Redis fixed-window counters (shared across replicas) when available
  2. In-process buckets (per-instance only) as a graceful fallback

The shared-Redis path is the production default — in-memory only kicks in
during dev when Redis isn't running, so a single replica still gets sane
limits without a hard dependency.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Callable, Optional

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.services.cache import cache_service

logger = logging.getLogger(__name__)

# Paths that should never be rate-limited (operational endpoints + static assets)
_EXEMPT_PATHS = {"/health", "/docs", "/redoc", "/openapi.json", "/", "/metrics"}
_EXEMPT_PREFIXES = ("/uploads/",)


class _RateLimited(Exception):
    """Internal signal: limit hit. Carries the user-facing detail + headers.

    NOTE: this is deliberately NOT fastapi.HTTPException — an HTTPException
    raised inside BaseHTTPMiddleware never reaches FastAPI's exception
    handlers (they live inside the middleware stack), so it used to surface
    as a 500 "Internal server error" instead of a 429.
    """

    def __init__(self, detail: str, retry_after: int):
        super().__init__(detail)
        self.detail = detail
        self.retry_after = retry_after


def _extract_user_id(request: Request) -> Optional[str]:
    """Resolve the user_id (`sub`) from the bearer header or auth cookie."""
    token: Optional[str] = None
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        token = auth.split(None, 1)[1].strip()
    if not token:
        # Browser sessions authenticate via the httpOnly cookie — without
        # this, every cookie-auth user shared one per-IP bucket (and NAT'd
        # offices starved each other).
        token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if not token or token == "guest":
        return None
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        sub = payload.get("sub")
        return sub if isinstance(sub, str) else None
    except JWTError:
        return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiter scoped per authenticated user (else per IP)."""

    def __init__(
        self,
        app,
        rate_per_minute: Optional[int] = None,
        rate_per_hour: Optional[int] = None,
    ):
        super().__init__(app)
        self.rate_per_minute = rate_per_minute or settings.RATE_LIMIT_PER_MINUTE
        self.rate_per_hour = rate_per_hour or settings.RATE_LIMIT_PER_HOUR
        # Fallback in-process buckets (used only when Redis is unavailable)
        self._minute_buckets: dict[str, list[float]] = defaultdict(list)
        self._hour_buckets: dict[str, list[float]] = defaultdict(list)
        self._last_prune = 0.0

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path in _EXEMPT_PATHS or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)

        identity = self._identity_key(request)
        try:
            minute_count, hour_count = await self._consume(identity)
        except _RateLimited as exc:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": exc.detail},
                headers={
                    "Retry-After": str(exc.retry_after),
                    "X-RateLimit-Limit": str(self.rate_per_minute),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.rate_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(max(0, self.rate_per_minute - minute_count))
        response.headers["X-RateLimit-Identity"] = identity.split(":", 1)[0]
        return response

    # ── identity ──────────────────────────────────────────────────────────────

    @staticmethod
    def _identity_key(request: Request) -> str:
        user_id = _extract_user_id(request)
        if user_id:
            return f"user:{user_id}"
        client_ip = request.client.host if request.client else "unknown"
        return f"ip:{client_ip}"

    # ── consumption ───────────────────────────────────────────────────────────

    async def _consume(self, identity: str) -> tuple[int, int]:
        """Returns (current minute count, current hour count) AFTER consuming this hit."""
        # Try Redis first — it's the source of truth in production
        if cache_service.redis is not None:
            try:
                return await self._consume_redis(identity)
            except _RateLimited:
                # A hit limit is the ANSWER, not a backend failure. This must
                # re-raise before the generic handler below: _RateLimited is an
                # Exception subclass, so `except Exception` used to swallow it
                # and fall through to the in-process buckets, whose counters
                # start empty. The effective limit became 2x the configured
                # rate (Redis allowed N, then local allowed another N) and
                # every rejection was mislogged as "Redis unavailable".
                raise
            except Exception as e:
                # If Redis genuinely hiccups, fall through to local — better
                # degraded than 500ing every request.
                logger.warning(f"Redis rate-limit unavailable, falling back to in-process: {e}")

        return self._consume_local(identity)

    async def _consume_redis(self, identity: str) -> tuple[int, int]:
        now = int(time.time())
        minute_window = now // 60
        hour_window = now // 3600
        minute_key = f"rl:m:{identity}:{minute_window}"
        hour_key = f"rl:h:{identity}:{hour_window}"

        # Pipeline so we incr+expire atomically and avoid a round-trip per command
        redis = cache_service.redis
        assert redis is not None
        async with redis.pipeline(transaction=False) as pipe:
            pipe.incr(minute_key, 1)
            pipe.expire(minute_key, 70)  # window + grace
            pipe.incr(hour_key, 1)
            pipe.expire(hour_key, 3700)
            results = await pipe.execute()

        minute_count = int(results[0] or 0)
        hour_count = int(results[2] or 0)

        if minute_count > self.rate_per_minute:
            logger.warning(f"Rate limit exceeded (minute) for {identity}")
            raise _RateLimited("Rate limit exceeded. Try again later.", 60 - (now % 60))
        if hour_count > self.rate_per_hour:
            logger.warning(f"Rate limit exceeded (hour) for {identity}")
            raise _RateLimited("Hourly rate limit exceeded.", 3600 - (now % 3600))
        return minute_count, hour_count

    def _consume_local(self, identity: str) -> tuple[int, int]:
        now = time.time()
        # Periodically drop identities whose window fully expired — without
        # this the dicts grow one entry per client IP forever (memory leak).
        if now - self._last_prune > 60:
            self._last_prune = now
            for buckets, window in ((self._minute_buckets, 60), (self._hour_buckets, 3600)):
                for key in [k for k, v in buckets.items() if not v or now - v[-1] >= window]:
                    del buckets[key]

        minute = self._minute_buckets[identity] = [
            t for t in self._minute_buckets[identity] if now - t < 60
        ]
        hour = self._hour_buckets[identity] = [
            t for t in self._hour_buckets[identity] if now - t < 3600
        ]

        if len(minute) >= self.rate_per_minute:
            logger.warning(f"Rate limit exceeded (minute, local) for {identity}")
            raise _RateLimited("Rate limit exceeded. Try again later.", 60)
        if len(hour) >= self.rate_per_hour:
            logger.warning(f"Rate limit exceeded (hour, local) for {identity}")
            raise _RateLimited("Hourly rate limit exceeded.", 3600)

        minute.append(now)
        hour.append(now)
        return len(minute), len(hour)
