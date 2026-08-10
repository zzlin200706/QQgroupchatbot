"""Async SQLAlchemy database lifecycle helpers."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.storage.models import Base


class Database:
    """Own the async engine and session factory for one application instance."""

    def __init__(self, url: str) -> None:
        _ensure_sqlite_parent_directory(url)
        self.engine: AsyncEngine = create_async_engine(url)
        if make_url(url).get_backend_name() == "sqlite":
            event.listen(self.engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def initialize(self) -> None:
        """Create the early-stage schema if it does not exist yet."""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        """Release database connections during application shutdown."""

        await self.engine.dispose()


def _ensure_sqlite_parent_directory(url: str) -> None:
    """Create a file-backed SQLite database parent directory when needed."""

    parsed_url = make_url(url)
    database = parsed_url.database
    if parsed_url.get_backend_name() != "sqlite" or not database or database == ":memory:":
        return
    Path(database).parent.mkdir(parents=True, exist_ok=True)


def _enable_sqlite_foreign_keys(dbapi_connection: object, connection_record: object) -> None:
    """Make declared foreign keys effective for every SQLite connection."""

    del connection_record
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
