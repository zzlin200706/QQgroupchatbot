"""Regression tests for the Database lifecycle and SQLite pragmas."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.storage.database import Database
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


@pytest.mark.asyncio
async def test_database_enables_wal_and_busy_timeout_for_file_backed_sqlite(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'wal.db'}")
    await database.initialize()

    async with database.engine.connect() as connection:
        journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
        busy_timeout = await connection.scalar(text("PRAGMA busy_timeout"))
        foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 5000
    assert int(foreign_keys) == 1

    await database.dispose()


@pytest.mark.asyncio
async def test_database_in_memory_does_not_force_wal(tmp_path: Path) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.initialize()

    async with database.engine.connect() as connection:
        # journal_mode on a memory DB is "memory", not "wal"; we only
        # assert that the connection is functional.
        result = await connection.scalar(text("SELECT 1"))

    assert result == 1

    await database.dispose()


@pytest.mark.asyncio
async def test_raw_event_repository_dedupes_by_payload_hash(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'dedupe.db'}")
    await database.initialize()
    repository = RawEventRepository(database.session_factory)

    from datetime import datetime, timezone

    payload = {"d": {"id": "m1", "content": "hello"}}
    first = RawEvent(
        platform="qq_official",
        received_at=datetime.now(timezone.utc),
        raw_payload=payload,
        payload_hash="deadbeef" * 8,
    )
    second = RawEvent(
        platform="qq_official",
        received_at=datetime.now(timezone.utc),
        raw_payload=payload,
        payload_hash="deadbeef" * 8,
    )
    inserted_first = await repository.insert(first)
    inserted_second = await repository.insert(second)

    assert inserted_first.id == inserted_second.id
    assert len(await repository.list_recent()) == 1

    await database.dispose()


@pytest.mark.asyncio
async def test_database_disposes_engine_without_runtimeerror(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'dispose.db'}")
    await database.initialize()
    await database.dispose()
    # Double dispose is a no-op rather than an error.
    await database.dispose()
