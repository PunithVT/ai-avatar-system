"""
Structured logging setup.

Plain text logs are fine for local development (they're easy for humans to
read), but production needs JSON so log-aggregation systems (Loki, Datadog,
CloudWatch, etc.) can filter and aggregate fields. This module picks the
right format based on `settings.ENVIRONMENT`.

Every record gets a `request_id` field populated from the contextvar set by
RequestLoggingMiddleware, which lets you grep a single trace through STT →
LLM → TTS even when those run in separate threads.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Mapping

from app.config import settings
from app.middleware.security import current_request_id


# Keys that LogRecord populates that we don't want to emit twice.
_STANDARD_RECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JSONFormatter(logging.Formatter):
    """One JSON object per line, with `extra={}` fields surfaced flat."""

    def format(self, record: logging.LogRecord) -> str:
        # `record.getMessage()` applies %-style args without forcing the
        # caller to pre-format — preserves the structured-logging pattern.
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03d%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": current_request_id(),
        }

        # Surface every `extra={...}` kwarg as a top-level field. Anything in
        # _STANDARD_RECORD_KEYS came from LogRecord itself, not the caller.
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_KEYS or key.startswith("_"):
                continue
            # Best-effort JSON-serializable conversion; fall back to str().
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = str(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    """Human-readable formatter for local dev — always includes the request_id."""

    def format(self, record: logging.LogRecord) -> str:
        rid = current_request_id()
        rid_part = f" [{rid}]" if rid and rid != "-" else ""
        # No exotic args parsing here — we mirror the previous format so dev
        # console output looks the same as before, just with a request id.
        base = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} - {record.name} - "
            f"{record.levelname}{rid_part} - {record.getMessage()}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging() -> None:
    """
    Idempotent root-logger setup. Call exactly once at process start.
    Safe to call again — replaces existing handlers.
    """
    use_json = settings.ENVIRONMENT == "production"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter() if use_json else PlainFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))

    # Tame noisy third-party loggers so production logs aren't swamped.
    for noisy in ("uvicorn.access", "watchfiles", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_extra(**fields: Any) -> Mapping[str, Any]:
    """
    Convenience: `logger.info("foo", extra=log_extra(user_id=u, latency_ms=5))`.
    Just an alias for clarity — Python's `extra=` already accepts a dict.
    """
    return fields
