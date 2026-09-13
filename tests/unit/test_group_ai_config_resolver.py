from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.group_ai_config_resolver import (
    GroupAIConfigResolver,
    GroupAIConfigSnapshot,
)
from app.storage.database import Database
from app.storage.group_ai_config_repository import GroupAIConfigRepository


PLATFORM = "qq_official"
GROUP_A = "group-a"
NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


async def _repository_with_db(
    tmp_path: Path,
) -> tuple[GroupAIConfigRepository, Database]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'resolver.db'}")
    await database.initialize()
    return (
        GroupAIConfigRepository(database.session_factory, clock=lambda: NOW),
        database,
    )


@pytest.mark.asyncio
async def test_resolve_returns_default_snapshot_when_no_row_exists(
    tmp_path: Path,
) -> None:
    repository, database = await _repository_with_db(tmp_path)
    try:
        resolver = GroupAIConfigResolver(repository)
        snapshot = await resolver.resolve(platform=PLATFORM, group_id=GROUP_A)
        assert snapshot == GroupAIConfigSnapshot(
            enabled=None, summary_max_input_chars=None
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_resolve_returns_overrides_when_row_exists(tmp_path: Path) -> None:
    repository, database = await _repository_with_db(tmp_path)
    try:
        await repository.upsert(
            PLATFORM, GROUP_A, enabled=False, summary_max_input_chars=50_000
        )
        resolver = GroupAIConfigResolver(repository)
        snapshot = await resolver.resolve(platform=PLATFORM, group_id=GROUP_A)
        assert snapshot.enabled is False
        assert snapshot.summary_max_input_chars == 50_000
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_resolve_returns_partial_overrides(tmp_path: Path) -> None:
    repository, database = await _repository_with_db(tmp_path)
    try:
        # Only the ``enabled`` knob is overridden.
        await repository.upsert(PLATFORM, GROUP_A, enabled=False)
        resolver = GroupAIConfigResolver(repository)
        snapshot = await resolver.resolve(platform=PLATFORM, group_id=GROUP_A)
        assert snapshot.enabled is False
        assert snapshot.summary_max_input_chars is None
    finally:
        await database.dispose()


def test_effective_enabled_uses_override_when_present() -> None:
    snapshot = GroupAIConfigSnapshot(
        enabled=False, summary_max_input_chars=None
    )
    assert (
        GroupAIConfigResolver.effective_enabled(
            snapshot, global_enabled=True
        )
        is False
    )


def test_effective_enabled_falls_back_to_global_when_no_override() -> None:
    snapshot = GroupAIConfigSnapshot(
        enabled=None, summary_max_input_chars=None
    )
    assert (
        GroupAIConfigResolver.effective_enabled(
            snapshot, global_enabled=True
        )
        is True
    )
    assert (
        GroupAIConfigResolver.effective_enabled(
            snapshot, global_enabled=False
        )
        is False
    )


def test_effective_summary_max_input_chars_uses_override_when_present() -> None:
    snapshot = GroupAIConfigSnapshot(
        enabled=None, summary_max_input_chars=20_000
    )
    assert (
        GroupAIConfigResolver.effective_summary_max_input_chars(
            snapshot, default=120_000
        )
        == 20_000
    )


def test_effective_summary_max_input_chars_falls_back_to_default() -> None:
    snapshot = GroupAIConfigSnapshot(
        enabled=None, summary_max_input_chars=None
    )
    assert (
        GroupAIConfigResolver.effective_summary_max_input_chars(
            snapshot, default=120_000
        )
        == 120_000
    )
