import time
import logging
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Token bucket rate limiter per IP address.
    Limits are configurable via settings.
    """

    def __init__(self, app, rate_per_minute: int = None, rate_per_hour: int = None):
        super().__init__(app)
        self.rate_per_minute = rate_per_minute or settings.RATE_LIMIT_PER_MINUTE
        self.rate_per_hour = rate_per_hour or settings.RATE_LIMIT_PER_HOUR
        self._minute_buckets: dict = defaultdict(list)
        self._hour_buckets: dict = defaultdict(list)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip rate limiting for health checks and docs
        if request.url.path in ("/health", "/docs", "/redoc", "/openapi.json", "/"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Clean old entries and check minute limit
        self._minute_buckets[client_ip] = [
            t for t in self._minute_buckets[client_ip] if now - t < 60
        ]
        if len(self._minute_buckets[client_ip]) >= self.rate_per_minute:
            logger.warning(f"Rate limit exceeded (minute) for {client_ip}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again later.",
                headers={"Retry-After": "60"},
            )

        # Check hour limit
        self._hour_buckets[client_ip] = [
            t for t in self._hour_buckets[client_ip] if now - t < 3600
        ]
        if len(self._hour_buckets[client_ip]) >= self.rate_per_hour:
            logger.warning(f"Rate limit exceeded (hour) for {client_ip}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again later.",
                headers={"Retry-After": "3600"},
            )

        # Record this request
        self._minute_buckets[client_ip].append(now)
        self._hour_buckets[client_ip].append(now)

        response = await call_next(request)

        # Add rate limit headers
        response.headers["X-RateLimit-Limit"] = str(self.rate_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(
            self.rate_per_minute - len(self._minute_buckets[client_ip])
        )

        return response
