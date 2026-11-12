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
    from app.config import settings

    async def no_load(session_id):
        return None

    monkeypatch.setattr(wsmod.websocket_manager, "_load_session_data", no_load)
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "clips")
    token = create_access_token(data={"sub": "owner"})
    with TestClient(main.app) as tc:
        with tc.websocket_connect(f"/ws/session/s1?token={token}") as ws:
            frame = ws.receive_json()
            assert frame["type"] == "ready"
            # With the default transport the frame offers nothing new: no
            # WebRTC, so the client carries on exactly as it always has.
            assert frame["media"]["webrtc"] is False


async def test_the_ready_frame_offers_webrtc_only_when_it_is_configured(seeded, monkeypatch):
    import app.websocket as wsmod
    from app.config import settings

    async def no_load(session_id):
        return None

    monkeypatch.setattr(wsmod.websocket_manager, "_load_session_data", no_load)
    token = create_access_token(data={"sub": "owner"})
    turn = '[{"urls": ["turn:turn.example.com:3478"], "username": "u", "credential": "p"}]'
    monkeypatch.setattr(settings, "WEBRTC_ICE_SERVERS", turn)
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "clips")

    def ready() -> dict:
        with TestClient(main.app) as tc:
            with tc.websocket_connect(f"/ws/session/s1?token={token}") as ws:
                return ws.receive_json()

    servers = [{"urls": ["turn:turn.example.com:3478"], "username": "u", "credential": "p"}]
    # `mic` (will the server take the microphone over the stream and decide
    # the turns?) can only be true where there is a stream to carry it.
    monkeypatch.setattr(settings, "VOICE_TURN_DETECTION", "smart")
    assert ready() == {
        "type": "ready",
        "media": {"webrtc": False, "ice_servers": servers, "mic": False},
    }
    monkeypatch.setattr(settings, "MEDIA_TRANSPORT", "webrtc")
    from app.services.turn_taking import turn_taking_available

    assert ready() == {
        "type": "ready",
        "media": {"webrtc": True, "ice_servers": servers, "mic": turn_taking_available()},
    }
    monkeypatch.setattr(settings, "VOICE_TURN_DETECTION", "off")
    assert ready() == {
        "type": "ready",
        "media": {"webrtc": True, "ice_servers": servers, "mic": False},
    }


async def test_signalling_frames_are_dispatched_and_never_drop_the_socket(seeded, monkeypatch):
    """An offer the server cannot honour is answered, and the connection lives on."""
    import app.websocket as wsmod

    async def no_load(session_id):
        return None

    monkeypatch.setattr(wsmod.websocket_manager, "_load_session_data", no_load)
    monkeypatch.setattr(wsmod.settings, "MEDIA_TRANSPORT", "clips")
    token = create_access_token(data={"sub": "owner"})
    with TestClient(main.app) as tc:
        with tc.websocket_connect(f"/ws/session/s1?token={token}") as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "webrtc_offer", "sdp": "v=0"})
            assert ws.receive_json()["type"] == "webrtc_error"
            ws.send_json({"type": "webrtc_offer"})
            assert ws.receive_json()["type"] == "webrtc_error"
            ws.send_json({"type": "webrtc_close"})
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


async def test_a_mic_frame_on_the_default_transport_is_answered_and_changes_nothing(
    seeded, monkeypatch
):
    """A client that asks anyway is told no; the server builds nothing for it."""
    import app.websocket as wsmod

    async def no_load(session_id):
        return None

    monkeypatch.setattr(wsmod.websocket_manager, "_load_session_data", no_load)
    monkeypatch.setattr(wsmod.settings, "MEDIA_TRANSPORT", "clips")
    token = create_access_token(data={"sub": "owner"})
    with TestClient(main.app) as tc:
        with tc.websocket_connect(f"/ws/session/s1?token={token}") as ws:
            assert ws.receive_json()["media"]["mic"] is False
            ws.send_json({"type": "mic", "active": True})
            assert ws.receive_json() == {"type": "mic_state", "listening": False}
            ws.send_json({"type": "mic", "active": False})
            assert ws.receive_json() == {"type": "mic_state", "listening": False}
            ws.send_json({"type": "mic"})
            assert ws.receive_json() == {"type": "mic_state", "listening": False}
            assert wsmod.websocket_manager._mics == {}
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
