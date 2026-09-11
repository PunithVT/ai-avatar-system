import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_root_endpoint(client: AsyncClient):
    """Test the root endpoint returns API info."""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "AI Avatar System API"
    assert data["version"] == "2.0.0"
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    """
    The probe reports its shape and stays serving.

    200 covers both "healthy" and "degraded" — CI runs without an LLM API
    key, which is a configuration problem rather than a reason to take the
    instance out of rotation. Only a hard-dependency failure yields 503; see
    test_health_returns_503_when_a_dependency_is_down.
    """
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("healthy", "degraded")
    assert "services" in data
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_docs_visibility_follows_debug(client: AsyncClient):
    """
    Interactive docs are exposed only when DEBUG is on.

    This previously asserted the docs were always reachable, which only held
    because the dev machine runs DEBUG=true — CI sets DEBUG=false, where
    serving /docs would leak the full API surface. The test had never
    actually run in CI (the job died installing dependencies), so the
    mismatch stayed hidden. Assert against the real setting instead.
    """
    from app.config import settings

    response = await client.get("/docs")
    if settings.DEBUG:
        assert response.status_code in (200, 307)
    else:
        assert response.status_code == 404, "docs must not be served when DEBUG is off"
