"""
Cross-cutting middleware: security headers, request/response logging,
and per-request correlation IDs.

`request_id_var` is a context variable read by the logging layer so every
log line emitted while serving a request includes the same `request_id`,
which lets you trace a single user action through STT → LLM → TTS →
animation across log streams.
"""

import contextvars
import logging
import time
import uuid
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger(__name__)

# ContextVar so async tasks spawned during the request (background TTS jobs,
# DB writes via to_thread, etc.) inherit the correlation id automatically.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def current_request_id() -> str:
    """Return the request id of the currently-serving request, or '-'."""
    return request_id_var.get()


# Content Security Policy. Keep `'unsafe-inline'` on styles because Tailwind
# injects a runtime stylesheet; everything else is locked down to same-origin.
# `connect-src` allows WSS because the chat surface streams over WebSocket.
# When you move to a CDN for static assets or fonts, add it here explicitly.
_BASE_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' blob: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data: https:; "
    "script-src 'self'; "
    "connect-src 'self' ws: wss: https:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply hardening headers to every response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # X-XSS-Protection is obsolete in modern browsers and a CSP is the
        # current defense, but legacy intermediaries still look for it.
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(self), geolocation=(), interest-cohort=()"
        )
        response.headers["Content-Security-Policy"] = _BASE_CSP
        # HSTS only when actually behind TLS — Strict-Transport-Security on a
        # plain-HTTP response will pin a broken policy if the proxy is later
        # misconfigured. Production environments terminate TLS upstream.
        if settings.ENVIRONMENT == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        # Modern Spectre/XS-Leaks mitigations
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-site"

        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Assign each request a UUID, propagate it via contextvars so downstream
    logs include it, and log a single structured line on completion.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Honor an upstream-provided X-Request-ID (a load balancer or sidecar
        # may already have one); otherwise mint a fresh hex id. The token form
        # is short enough to be human-friendly in logs.
        incoming = request.headers.get("x-request-id")
        request_id = incoming if incoming and len(incoming) <= 64 else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        start = time.time()

        try:
            response = await call_next(request)
        finally:
            duration = time.time() - start
            request_id_var.reset(token)

        client = request.client.host if request.client else "-"
        # logger.info with extras is consumed by the JSON formatter (see
        # app.logging_config) and emerges as a single structured record.
        logger.info(
            "request_complete",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration * 1000, 2),
                "client": client,
            },
        )

        response.headers["X-Response-Time"] = f"{duration:.3f}s"
        response.headers["X-Request-ID"] = request_id
        return response
