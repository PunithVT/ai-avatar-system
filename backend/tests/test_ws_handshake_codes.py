"""
What a refused WebSocket handshake tells the client.

Three different situations used to produce one close code (4401), and the
client treats 4401 as "your login is no longer valid" and signs the user out.
So deleting a conversation in another tab, or the database being briefly
unreachable during a reconnect, logged the user out — and a guest, whose token
is the only way back into the account, lost it permanently.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main
from app.database import Base
from app.models import Avatar, User
from app.models import Session as SessionModel
from app.tokens import create_access_token


@pytest.fixture
async def seeded(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'ws.db'}"
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as db:
        db.add_all(
            [
                User(id="owner", email="o@example.com", username="owner", hashed_password="x"),
                User(id="other", email="x@example.com", username="other", hashed_password="x"),
                Avatar(
                    id="a1",
                    user_id="owner",
                    name="A",
                    image_url="http://x/i.jpg",
                    s3_key="avatars/a1/image.jpg",
                    status="ready",
                ),
                SessionModel(id="s1", user_id="owner", avatar_id="a1", status="active"),
            ]
        )
        await db.commit()
    monkeypatch.setattr("app.database.AsyncSessionLocal", sm)
    monkeypatch.setattr(main, "AsyncSessionLocal", sm)
    yield
    await engine.dispose()


def _close_code(path: str) -> int:
    with TestClient(main.app) as tc:
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect(path) as ws:
                ws.receive_json()
        return exc.value.code


async def test_missing_or_invalid_token_is_4401(seeded):
    assert _close_code("/ws/session/s1") == main.WS_CLOSE_UNAUTHENTICATED == 4401
    assert _close_code("/ws/session/s1?token=not-a-jwt") == 4401


async def test_unknown_session_is_not_reported_as_an_auth_failure(seeded):
    token = create_access_token(data={"sub": "owner"})
    assert _close_code(f"/ws/session/deleted-session?token={token}") == 4404


async def test_someone_elses_session_looks_the_same_as_a_missing_one(seeded):
    """Distinguishing them would confirm to a stranger that the id exists."""
    token = create_access_token(data={"sub": "other"})
    assert _close_code(f"/ws/session/s1?token={token}") == 4404


async def test_a_failed_lookup_is_retryable_not_a_logout(seeded, monkeypatch):
    def broken():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main, "AsyncSessionLocal", broken)
    token = create_access_token(data={"sub": "owner"})
    assert _close_code(f"/ws/session/s1?token={token}") == 1011


async def test_an_accepted_connection_announces_itself_first(seeded, monkeypatch):
    """
    Refusals are accepted-then-closed, so the socket opening proves nothing;
    the client resets its reconnect backoff on this frame instead.
    """
    import app.websocket as wsmod

    async def no_load(session_id):
        return None

    monkeypatch.setattr(wsmod.websocket_manager, "_load_session_data", no_load)
    token = create_access_token(data={"sub": "owner"})
    with TestClient(main.app) as tc:
        with tc.websocket_connect(f"/ws/session/s1?token={token}") as ws:
            assert ws.receive_json() == {"type": "ready"}
