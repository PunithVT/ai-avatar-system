import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_avatars(client: AsyncClient, auth_headers):
    """Test listing avatars endpoint."""
    response = await client.get("/api/v1/avatars/", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_get_avatar_not_found(client: AsyncClient, auth_headers):
    """Test getting a non-existent avatar."""
    response = await client.get("/api/v1/avatars/nonexistent-id", headers=auth_headers)
    assert response.status_code == 404
