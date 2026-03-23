import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_sessions(client: AsyncClient):
    """Test listing sessions endpoint."""
    response = await client.get("/api/v1/sessions/")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_session_not_found(client: AsyncClient):
    """Test getting a non-existent session."""
    response = await client.get("/api/v1/sessions/nonexistent-id")
    assert response.status_code == 404
