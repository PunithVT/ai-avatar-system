"""
Regression tests for the upload / health / middleware / config hardening pass.
"""

import io

import pytest
from httpx import AsyncClient
from PIL import Image

from app.config import Settings, settings


def _png(size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (120, 90, 200)).save(buf, format="PNG")
    return buf.getvalue()


# ── upload limits are enforced from configuration ────────────────────────
async def test_oversize_upload_is_rejected_with_413(client: AsyncClient, auth_headers, monkeypatch):
    """
    The size check used to run *after* `await file.read()`, so the whole body
    was buffered before being rejected — an attacker-controlled allocation.
    It is now enforced while streaming, and reported as 413 not 400.
    """
    monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE", 256 * 1024)
    r = await client.post(
        "/api/v1/avatars/upload",
        data={"name": "big"},
        files={"file": ("big.png", b"\x89PNG\r\n\x1a\n" + b"A" * (512 * 1024), "image/png")},
        headers=auth_headers,
    )
    assert r.status_code == 413, r.text


async def test_upload_size_limit_follows_the_setting(
    client: AsyncClient, auth_headers, monkeypatch
):
    """MAX_UPLOAD_SIZE was declared but ignored — the limit was hardcoded."""
    payload = _png((256, 256))
    monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE", len(payload) - 1)
    r = await client.post(
        "/api/v1/avatars/upload",
        data={"name": "n"},
        files={"file": ("a.png", payload, "image/png")},
        headers=auth_headers,
    )
    assert r.status_code == 413, "lowering MAX_UPLOAD_SIZE must actually tighten the limit"


@pytest.mark.parametrize(
    "filename,expected_reject", [("a.png", False), ("a.svg", True), ("a.tiff", True)]
)
async def test_extension_allowlist_is_applied(
    client: AsyncClient, auth_headers, filename, expected_reject
):
    """ALLOWED_EXTENSIONS was declared but never consulted."""
    r = await client.post(
        "/api/v1/avatars/upload",
        data={"name": "n"},
        files={"file": (filename, _png(), "image/png")},
        headers=auth_headers,
    )
    if expected_reject:
        assert r.status_code == 400, f"{filename} should be refused by the allowlist"
        assert "Allowed" in r.text
    else:
        assert r.status_code != 400, r.text


async def test_avatar_name_is_length_capped_like_rename(client: AsyncClient, auth_headers):
    """Upload accepted an unbounded name while /name capped it at 200."""
    r = await client.post(
        "/api/v1/avatars/upload",
        data={"name": "N" * (settings.MAX_AVATAR_NAME_LEN + 1)},
        files={"file": ("a.png", _png(), "image/png")},
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text


# ── /health reports failure in the status code ────────────────────────────
async def test_health_returns_503_when_a_dependency_is_down(client: AsyncClient, monkeypatch):
    """
    Orchestrators read the status code, not the body. /health used to answer
    200 with {"status": "degraded"} while Postgres was unreachable, so a
    broken instance kept receiving traffic.
    """
    import main

    class _Boom:
        def connect(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(main, "engine", _Boom())
    r = await client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["services"]["database"] == "disconnected"


async def test_health_is_200_when_healthy(client: AsyncClient):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["services"]["database"] == "connected"


# ── middleware ordering ──────────────────────────────────────────────────
def test_security_headers_are_outermost():
    """
    add_middleware inserts at the front, so the last registration is the
    outermost layer. SecurityHeaders must be outermost to cover responses
    that short-circuit in outer layers (rate-limit 429s, CORS preflights),
    and CORS must sit outside the rate limiter so a 429 is readable by a
    browser instead of surfacing as an opaque CORS failure.
    """
    from main import app

    names = [m.cls.__name__ for m in app.user_middleware]
    order = {n: i for i, n in enumerate(names)}  # lower index == more outer
    assert order["SecurityHeadersMiddleware"] < order["RequestLoggingMiddleware"]
    assert order["RequestLoggingMiddleware"] < order["CORSMiddleware"]
    assert order["CORSMiddleware"] < order["RateLimitMiddleware"]


async def test_security_headers_present_on_success(client: AsyncClient):
    r = await client.get("/health")
    for header in (
        "X-Content-Type-Options",
        "Content-Security-Policy",
        "Referrer-Policy",
        "Cross-Origin-Opener-Policy",
    ):
        assert header in r.headers, f"{header} missing"


# ── production configuration is validated at startup ─────────────────────
_PROD_BASE = dict(
    ENVIRONMENT="production",
    SECRET_KEY="s" * 40,
    JWT_SECRET_KEY="j" * 40,
)


@pytest.mark.parametrize(
    "overrides,should_fail,because",
    [
        (
            dict(DEBUG=True, AUTH_COOKIE_SECURE=True, CORS_ORIGINS=["https://x.com"]),
            True,
            "DEBUG unlocks docs, create_all, raw 5xx bodies and SQL echo",
        ),
        (
            dict(DEBUG=False, AUTH_COOKIE_SECURE=False, CORS_ORIGINS=["https://x.com"]),
            True,
            "auth cookie would be sent over plain HTTP",
        ),
        (
            dict(DEBUG=False, AUTH_COOKIE_SECURE=True, CORS_ORIGINS=["http://x.com"]),
            True,
            "plain-HTTP origin in production",
        ),
        (
            dict(DEBUG=False, AUTH_COOKIE_SECURE=True, CORS_ORIGINS=["*"]),
            True,
            "wildcard CORS in production",
        ),
        (
            dict(DEBUG=False, AUTH_COOKIE_SECURE=True, CORS_ORIGINS=["https://x.com"]),
            False,
            "correctly hardened",
        ),
    ],
)
def test_production_config_is_validated(overrides, should_fail, because):
    kwargs = {**_PROD_BASE, **overrides}
    if should_fail:
        with pytest.raises(Exception) as exc:
            Settings(_env_file=None, **kwargs)
        assert "Unsafe production configuration" in str(exc.value), because
    else:
        Settings(_env_file=None, **kwargs)


def test_development_config_is_not_constrained():
    """The hardening rules must only apply to ENVIRONMENT=production."""
    Settings(
        _env_file=None,
        ENVIRONMENT="development",
        DEBUG=True,
        AUTH_COOKIE_SECURE=False,
        CORS_ORIGINS=["http://localhost:3000"],
        SECRET_KEY="s" * 40,
        JWT_SECRET_KEY="j" * 40,
    )


# ── WebSocket connection cap ─────────────────────────────────────────────
async def test_ws_connection_cap_is_enforced(monkeypatch):
    """
    WS_MAX_CONNECTIONS was declared in config but never read, so the number
    of live sockets — each pinning session state, a send lock, a private temp
    dir and possibly a GPU pipeline — was unbounded.
    """
    from app.websocket import websocket_manager

    monkeypatch.setattr(settings, "WS_MAX_CONNECTIONS", 2)
    monkeypatch.setattr(websocket_manager, "active_connections", {"a": object(), "b": object()})
    assert await websocket_manager.at_capacity() is True

    monkeypatch.setattr(websocket_manager, "active_connections", {"a": object()})
    assert await websocket_manager.at_capacity() is False


async def test_ws_keepalive_task_is_started_and_stopped():
    """
    WS_PING_INTERVAL / WS_PING_TIMEOUT were also dead settings, so a client
    that vanished without a close handshake held its slot until the two-hour
    stale reaper noticed.
    """
    from app.websocket import websocket_manager

    websocket_manager.start_cleanup_task()
    try:
        assert websocket_manager._keepalive_task is not None
        assert not websocket_manager._keepalive_task.done()
    finally:
        await websocket_manager.stop_cleanup_task()
    assert websocket_manager._keepalive_task is None
    assert websocket_manager._cleanup_task is None


# ── rate limiter must not leak past the configured rate ──────────────────
async def test_redis_limiter_does_not_fall_through_to_local_buckets(monkeypatch):
    """
    `_consume_redis` signals a hit limit by raising `_RateLimited`, which is
    an Exception subclass. The Redis-failure handler caught bare `Exception`,
    so it swallowed that signal and retried against the in-process buckets —
    whose counters start empty. The result was an effective limit of exactly
    2x the configured rate (Redis allowed N, then local allowed another N),
    with every rejection mislogged as "Redis unavailable".

    This ran in the default production configuration, where Redis is up.
    """
    from app.middleware.rate_limiter import RateLimitMiddleware, _RateLimited

    mw = RateLimitMiddleware(app=None, rate_per_minute=5, rate_per_hour=1000)

    # Stand in for a healthy Redis whose counter has already passed the limit.
    calls = {"redis": 0, "local": 0}

    class _FakeRedis:
        pass

    monkeypatch.setattr("app.middleware.rate_limiter.cache_service.redis", _FakeRedis())

    async def _fake_redis_consume(identity):
        calls["redis"] += 1
        raise _RateLimited("Rate limit exceeded. Try again later.", 30)

    def _fake_local_consume(identity):
        calls["local"] += 1
        return (1, 1)

    monkeypatch.setattr(mw, "_consume_redis", _fake_redis_consume)
    monkeypatch.setattr(mw, "_consume_local", _fake_local_consume)

    with pytest.raises(_RateLimited):
        await mw._consume("ip:test")

    assert calls["redis"] == 1
    assert calls["local"] == 0, "a hit limit must not fall through to the local buckets"


async def test_genuine_redis_failure_still_falls_back(monkeypatch):
    """The fallback must survive for real Redis errors — only _RateLimited re-raises."""
    from app.middleware.rate_limiter import RateLimitMiddleware

    mw = RateLimitMiddleware(app=None, rate_per_minute=5, rate_per_hour=1000)

    class _FakeRedis:
        pass

    monkeypatch.setattr("app.middleware.rate_limiter.cache_service.redis", _FakeRedis())

    async def _boom(identity):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(mw, "_consume_redis", _boom)
    monkeypatch.setattr(mw, "_consume_local", lambda identity: (3, 3))

    assert await mw._consume("ip:test") == (3, 3)
