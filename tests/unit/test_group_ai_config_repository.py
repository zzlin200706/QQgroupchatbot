from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.storage.database import Database
from app.storage.group_ai_config_repository import GroupAIConfigRepository


PLATFORM = "qq_official"
GROUP_A = "group-a"
GROUP_B = "group-b"
NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


async def _new_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'group-config.db'}")
    await database.initialize()
    return database


@pytest.mark.asyncio
async def test_get_returns_none_when_no_row(tmp_path: Path) -> None:
    database = await _new_database(tmp_path)
    try:
        repository = GroupAIConfigRepository(
            database.session_factory, clock=lambda: NOW
        )
        assert await repository.get(PLATFORM, GROUP_A) is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_upsert_creates_new_row(tmp_path: Path) -> None:
    database = await _new_database(tmp_path)
    try:
        repository = GroupAIConfigRepository(
            database.session_factory, clock=lambda: NOW
        )
        record = await repository.upsert(
            PLATFORM,
            GROUP_A,
            enabled=True,
            summary_max_input_chars=80_000,
        )
        assert record.platform == PLATFORM
        assert record.group_id == GROUP_A
        assert record.enabled is True
        assert record.summary_max_input_chars == 80_000
        assert record.updated_at == NOW
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_upsert_updates_existing_row(tmp_path: Path) -> None:
    database = await _new_database(tmp_path)
    try:
        repository = GroupAIConfigRepository(
            database.session_factory, clock=lambda: NOW
        )
        await repository.upsert(
            PLATFORM, GROUP_A, enabled=True, summary_max_input_chars=80_000
        )
        later = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        repository_later = GroupAIConfigRepository(
            database.session_factory, clock=lambda: later
        )
        updated = await repository_later.upsert(
            PLATFORM, GROUP_A, enabled=False, summary_max_input_chars=40_000
        )
        assert updated.enabled is False
        assert updated.summary_max_input_chars == 40_000
        assert updated.updated_at == later
        # And the row is the same physical row.
        assert await repository_later.get(PLATFORM, GROUP_A) is not None
        row = await repository_later.get(PLATFORM, GROUP_A)
        assert row.id == updated.id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_upsert_clears_knob_with_none(tmp_path: Path) -> None:
    database = await _new_database(tmp_path)
    try:
        repository = GroupAIConfigRepository(
            database.session_factory, clock=lambda: NOW
        )
        await repository.upsert(
            PLATFORM, GROUP_A, enabled=True, summary_max_input_chars=80_000
        )
        cleared = await repository.upsert(
            PLATFORM, GROUP_A, enabled=None, summary_max_input_chars=None
        )
        assert cleared.enabled is None
        assert cleared.summary_max_input_chars is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_group_config_isolated_per_group(tmp_path: Path) -> None:
    database = await _new_database(tmp_path)
    try:
        repository = GroupAIConfigRepository(
            database.session_factory, clock=lambda: NOW
        )
        await repository.upsert(
            PLATFORM, GROUP_A, enabled=True, summary_max_input_chars=80_000
        )
        await repository.upsert(
            PLATFORM, GROUP_B, enabled=False, summary_max_input_chars=10_000
        )
        a = await repository.get(PLATFORM, GROUP_A)
        b = await repository.get(PLATFORM, GROUP_B)
        assert a is not None and a.enabled is True
        assert a.summary_max_input_chars == 80_000
        assert b is not None and b.enabled is False
        assert b.summary_max_input_chars == 10_000
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_config_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.initialize()
    try:
        repository = GroupAIConfigRepository(
            database.session_factory, clock=lambda: NOW
        )
        await repository.upsert(
            PLATFORM, GROUP_A, enabled=True, summary_max_input_chars=80_000
        )
    finally:
        await database.dispose()

    restarted = Database(f"sqlite+aiosqlite:///{path}")
    await restarted.initialize()
    try:
        repository = GroupAIConfigRepository(
            restarted.session_factory, clock=lambda: NOW
        )
        record = await repository.get(PLATFORM, GROUP_A)
        assert record is not None
        assert record.enabled is True
        assert record.summary_max_input_chars == 80_000
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["platform", "group_id"])
async def test_get_rejects_empty_key(tmp_path: Path, field: str) -> None:
    database = await _new_database(tmp_path)
    try:
        repository = GroupAIConfigRepository(database.session_factory)
        key = {"platform": PLATFORM, "group_id": GROUP_A}
        key[field] = ""
        with pytest.raises(ValueError, match="must not be empty"):
            await repository.get(**key)
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["platform", "group_id"])
async def test_upsert_rejects_empty_key(tmp_path: Path, field: str) -> None:
    database = await _new_database(tmp_path)
    try:
        repository = GroupAIConfigRepository(database.session_factory)
        key = {"platform": PLATFORM, "group_id": GROUP_A}
        key[field] = ""
        with pytest.raises(ValueError, match="must not be empty"):
            await repository.upsert(**key, enabled=True)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_upsert_rejects_non_positive_max_input_chars(
    tmp_path: Path,
) -> None:
    database = await _new_database(tmp_path)
    try:
        repository = GroupAIConfigRepository(database.session_factory)
        with pytest.raises(ValueError, match="positive integer"):
            await repository.upsert(
                PLATFORM, GROUP_A, summary_max_input_chars=0
            )
        with pytest.raises(ValueError, match="positive integer"):
            await repository.upsert(
                PLATFORM, GROUP_A, summary_max_input_chars=-1
            )
    finally:
        await database.dispose()
