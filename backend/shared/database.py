from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Database:
    def __init__(self, url: str):
        kwargs = {}
        parsed = make_url(url)
        if parsed.get_backend_name() == "sqlite":
            if parsed.database and parsed.database != ":memory:":
                Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
            else:  # in-memory: every connection would be a separate empty database
                kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_tables(self) -> None:
        from .models import Base
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


_db: Database | None = None


def get_database() -> Database:
    global _db
    if _db is None:
        _db = Database(get_settings().database_url)
    return _db


def set_database(db: Database | None) -> None:
    """Swap the process-wide database (tests, host app)."""
    global _db
    _db = db


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request; handlers commit explicitly."""
    async with get_database().sessionmaker() as session:
        yield session
