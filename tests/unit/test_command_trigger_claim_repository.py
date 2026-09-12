from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.storage.command_trigger_claim_repository import CommandTriggerClaimRepository
from app.storage.database import Database


CLAIMED_AT = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_command_claim_is_exact_group_scoped_and_restart_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-claims.db"
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.initialize()
    repository = CommandTriggerClaimRepository(
        database.session_factory,
        clock=lambda: CLAIMED_AT,
    )
    key = {
        "platform": "qq_official",
        "group_id": "group-a",
        "trigger_message_id": "message-1",
        "command_name": "ping",
    }
    try:
        assert await repository.claim(**key) is True
        assert await repository.claim(**key) is False
        assert await repository.claim(**{**key, "group_id": "group-b"}) is True
        assert await repository.claim(**{**key, "platform": "other"}) is True
        assert await repository.claim(**{**key, "command_name": "summary"}) is True
    finally:
        await database.dispose()

    restarted = Database(f"sqlite+aiosqlite:///{path}")
    await restarted.initialize()
    restarted_repository = CommandTriggerClaimRepository(
        restarted.session_factory,
        clock=lambda: CLAIMED_AT,
    )
    try:
        assert await restarted_repository.claim(**key) is False
        assert await restarted_repository.exists(**key) is True
    finally:
        await restarted.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["platform", "group_id", "trigger_message_id", "command_name"],
)
async def test_command_claim_rejects_empty_key_parts(tmp_path: Path, field: str) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'invalid.db'}")
    await database.initialize()
    repository = CommandTriggerClaimRepository(database.session_factory)
    key = {
        "platform": "qq_official",
        "group_id": "group-a",
        "trigger_message_id": "message-1",
        "command_name": "ping",
    }
    key[field] = ""
    try:
        with pytest.raises(ValueError, match=field):
            await repository.claim(**key)
    finally:
        await database.dispose()
