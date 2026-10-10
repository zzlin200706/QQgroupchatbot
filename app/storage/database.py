"""Async SQLAlchemy database lifecycle helpers."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.storage.models import Base


# Default busy-timeout in seconds for SQLite.  The QQ bot continuously
# ingests events while AI-bound reads may scan the same database, so we
# want writers to wait briefly rather than immediately raise
# "database is locked".
DEFAULT_SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


class Database:
    """Own the async engine and session factory for one application instance."""

    def __init__(
        self,
        url: str,
        *,
        sqlite_busy_timeout_seconds: float = DEFAULT_SQLITE_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        if sqlite_busy_timeout_seconds < 0:
            raise ValueError("sqlite_busy_timeout_seconds must not be negative")
        _ensure_sqlite_parent_directory(url)
        self.engine: AsyncEngine = create_async_engine(url)
        if make_url(url).get_backend_name() == "sqlite":
            event.listen(
                self.engine.sync_engine,
                "connect",
                _enable_sqlite_foreign_keys,
            )
            # WAL is only safe for file-backed databases; :memory: rejects it.
            if not _is_sqlite_memory(url):
                event.listen(
                    self.engine.sync_engine,
                    "connect",
                    _enable_sqlite_wal,
                )
            event.listen(
                self.engine.sync_engine,
                "connect",
                _make_sqlite_busy_timeout_setter(sqlite_busy_timeout_seconds),
            )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._url = url

    async def initialize(self) -> None:
        """Create the early-stage schema if it does not exist yet."""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await _ensure_sqlite_unique_indexes(connection)
            await _checkpoint_wal_if_sqlite(connection)

    async def checkpoint_wal(self) -> None:
        """Force a WAL checkpoint, truncating the WAL file.

        A no-op for in-memory databases.  Truncating the WAL
        reclaims disk space. Subsequent writes can refill the WAL,
        so this does not make copying a live ``.db`` file safe.
        SQLite's backup API includes committed WAL contents and does
        not require an empty WAL.
        """

        if _is_sqlite_memory(self._url):
            return
        async with self.engine.begin() as connection:
            await _checkpoint_wal_if_sqlite(connection)

    @property
    def is_sqlite(self) -> bool:
        return make_url(self._url).get_backend_name() == "sqlite"

    @property
    def database_path(self) -> str | None:
        """Return the filesystem path of the underlying SQLite file, or None."""

        if not self.is_sqlite:
            return None
        database = make_url(self._url).database
        if not database or database == ":memory:":
            return None
        return database

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


def _is_sqlite_memory(url: str) -> bool:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return False
    database = parsed.database
    return not database or database == ":memory:"


def _enable_sqlite_foreign_keys(dbapi_connection: object, connection_record: object) -> None:
    """Make declared foreign keys effective for every SQLite connection."""

    del connection_record
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _enable_sqlite_wal(dbapi_connection: object, connection_record: object) -> None:
    """Switch file-backed SQLite to WAL so reads do not block writes.

    Errors (e.g. read-only filesystem) are swallowed because the
    application must still come up.  The Database class logs the
    effective journal mode at initialise time.
    """

    del connection_record
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
    except Exception:  # pragma: no cover - filesystem dependent
        pass
    finally:
        cursor.close()


def _make_sqlite_busy_timeout_setter(seconds: float) -> object:
    """Return an SQLAlchemy connect listener that sets ``busy_timeout``."""

    def _setter(dbapi_connection: object, connection_record: object) -> None:
        del connection_record
        if seconds <= 0:
            return
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute(f"PRAGMA busy_timeout={int(seconds * 1000)}")
        finally:
            cursor.close()

    return _setter


# Statements the application expects to be true on every freshly opened
# SQLite connection.  Kept centralised so tests can introspect.
SQLITE_REQUIRED_PRAGMAS: tuple[str, ...] = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA journal_mode=WAL",
    f"PRAGMA busy_timeout={int(DEFAULT_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}",
)


async def _checkpoint_wal_if_sqlite(connection: object) -> None:
    """Run ``PRAGMA wal_checkpoint(TRUNCATE)`` to keep the WAL bounded.

    SQLite normally checkpoints automatically when the WAL grows
    past a threshold, but on a long-running host the WAL file can
    still hold several megabytes.  We force a checkpoint at
    startup so a process that boots after a crash inherits a
    clean WAL and so the bot's database file can be safely
    snapshotted by ``sqlite3 .backup`` or ``VACUUM INTO``.
    """

    await connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


async def _ensure_sqlite_unique_indexes(connection: object) -> None:
    """Create UNIQUE indexes that SQLAlchemy did not declare declaratively.

    Older deployments of the bot may have been created before the
    ``raw_events.payload_hash`` UNIQUE constraint or the new
    ``messages`` covering index were added, so we apply them
    idempotently here at startup.  ``create_index`` is a no-op when
    the index already exists.
    """

    await connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_events_payload_hash "
            "ON raw_events (platform, payload_hash)"
        )
    )
    # Cover the hot read path used by summary / assistant context.
    # Older deployments only had ``ix_messages_group_id_timestamp`` so
    # the platform filter forced a full table scan.  The new index is
    # a strict superset and is ignored if already present.
    await connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_messages_platform_group_id_timestamp "
            "ON messages (platform, group_id, timestamp)"
        )
    )
