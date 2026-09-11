"""
Integration tests for the WebSocket handshake auth (`main._verify_ws_session`).

This is the security-critical gate that the ConnectionManager unit tests
don't cover: it talks to the real database (via AsyncSessionLocal) to confirm
the JWT's subject owns the session before any chat traffic is accepted. We
point AsyncSessionLocal at the per-test in-memory engine so the real code
path runs end-to-end without a live Postgres.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import main
from app.api.v1.users import create_access_token
from app.models import Avatar, Session


async def _seed_session(sessionmaker, user_id: str) -> str:
    async with sessionmaker() as db:
        avatar = Avatar(
            user_id=user_id,
            name="A",
            image_url="http://x/i.jpg",
            s3_key="avatars/x/image.jpg",
            status="ready",
        )
        db.add(avatar)
        await db.commit()
        await db.refresh(avatar)

        session = Session(user_id=user_id, avatar_id=avatar.id, status="active")
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session.id


@pytest.fixture
def patched_session_local(test_engine, monkeypatch):
    """Point main.AsyncSessionLocal at the test engine for the handshake path."""
    sm = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(main, "AsyncSessionLocal", sm)
    return sm


@pytest.mark.asyncio
async def test_valid_token_for_owned_session(patched_session_local):
    sid = await _seed_session(patched_session_local, "user-A")
    token = create_access_token(data={"sub": "user-A"})
    assert await main._verify_ws_session(sid, token) == "user-A"


@pytest.mark.asyncio
async def test_token_for_someone_elses_session_rejected(patched_session_local):
    sid = await _seed_session(patched_session_local, "user-A")
    # A valid token, but for a different user → must be rejected (no IDOR).
    token = create_access_token(data={"sub": "user-B"})
    assert await main._verify_ws_session(sid, token) is None


@pytest.mark.asyncio
async def test_unknown_session_rejected(patched_session_local):
    token = create_access_token(data={"sub": "user-A"})
    assert await main._verify_ws_session("does-not-exist", token) is None


@pytest.mark.asyncio
async def test_garbage_token_rejected(patched_session_local):
    sid = await _seed_session(patched_session_local, "user-A")
    assert await main._verify_ws_session(sid, "not-a-jwt") is None


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.asyncio
async def test_tokenless_handshake_always_rejected(patched_session_local, monkeypatch, debug):
    """
    A token is now required regardless of DEBUG.

    There used to be a DEBUG-only fallback that accepted a tokenless
    handshake for a session owned by the shared `demo-user`. That identity no
    longer exists — sessions cannot be created anonymously and guests get
    their own accounts — so an absent token is simply unauthenticated. Pinned
    for both DEBUG values because DEBUG is not enforced off outside
    production, so the dev path must be safe too.
    """
    sid = await _seed_session(patched_session_local, "user-A")
    monkeypatch.setattr(main.settings, "DEBUG", debug)
    assert await main._verify_ws_session(sid, None) is None


@pytest.mark.asyncio
async def test_demo_user_session_gets_no_special_treatment(patched_session_local, monkeypatch):
    """The literal id `demo-user` must not be privileged any more."""
    sid = await _seed_session(patched_session_local, "demo-user")
    monkeypatch.setattr(main.settings, "DEBUG", True)
    assert await main._verify_ws_session(sid, None) is None


@pytest.mark.asyncio
async def test_guest_token_works_for_its_own_session(patched_session_local):
    """A guest's JWT is an ordinary token and must authenticate normally."""
    sid = await _seed_session(patched_session_local, "guest-abc123")
    token = create_access_token(data={"sub": "guest-abc123"})
    assert await main._verify_ws_session(sid, token) == "guest-abc123"
