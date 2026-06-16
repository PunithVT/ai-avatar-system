from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import settings

# Create async engine. SQLite (NullPool) doesn't accept pool_size/max_overflow,
# which only apply to server-based DBs like Postgres.
_db_url = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")
_engine_kwargs = {"echo": settings.DEBUG, "pool_pre_ping": True}
if not _db_url.startswith("sqlite"):
    _engine_kwargs.update(pool_size=10, max_overflow=20)

engine = create_async_engine(_db_url, **_engine_kwargs)

# Create session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

# Create base class for models
Base = declarative_base()


async def get_db():
    """Dependency for getting database session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
