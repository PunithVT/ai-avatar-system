"""
Pytest fixtures for the API test suite.

Each test gets a completely fresh in-memory SQLite database (function-scoped
engine + StaticPool so the single in-memory connection is shared within the
test but discarded after it). This guarantees isolation — a user created in
one test can't collide with the same fixture in the next.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.users import create_access_token, get_password_hash
from app.database import Base, get_db
from app.models import User  # noqa: F401 — ensures models are registered on Base
from main import app

# Pure in-memory DB — no file on disk, fully isolated per engine instance.
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def test_engine():
    """Fresh in-memory engine + schema for every test (full isolation)."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine):
    """A session bound to the per-test engine."""
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(db_session, test_engine, monkeypatch):
    """
    Async test client with the DB dependency overridden to the test session.

    `main.engine` is also redirected at the per-test engine. It is a
    module-level pool pointed at the real Postgres, and asyncpg connections
    are bound to the event loop that created them — so /health (which uses
    `engine` directly rather than the injected session) would reuse a pooled
    connection from a previous test's loop and report the database as
    disconnected. That was invisible while /health always returned 200; now
    that a hard-dependency failure is a real 503, it has to be deterministic.
    """
    import main

    monkeypatch.setattr(main, "engine", test_engine)

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def test_user(db_session):
    """Create a test user in the per-test database."""
    user = User(
        email="test@example.com",
        username="testuser",
        full_name="Test User",
        hashed_password=get_password_hash("testpassword123"),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def auth_headers(test_user):
    """Authorization headers for the test user."""
    token = create_access_token(data={"sub": test_user.id})
    return {"Authorization": f"Bearer {token}"}
