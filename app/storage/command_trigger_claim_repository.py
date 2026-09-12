"""Durable idempotency claims for explicit inbound commands."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import CommandTriggerClaimRecord


class CommandTriggerClaimRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def claim(
        self,
        *,
        platform: str,
        group_id: str,
        trigger_message_id: str,
        command_name: str,
    ) -> bool:
        _validate_key(platform, group_id, trigger_message_id, command_name)
        record = CommandTriggerClaimRecord(
            platform=platform,
            group_id=group_id,
            trigger_message_id=trigger_message_id,
            command_name=command_name,
            claimed_at=self._clock(),
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(record)
                    await session.flush()
            return True
        except IntegrityError:
            if await self.exists(
                platform=platform,
                group_id=group_id,
                trigger_message_id=trigger_message_id,
                command_name=command_name,
            ):
                return False
            raise

    async def exists(
        self,
        *,
        platform: str,
        group_id: str,
        trigger_message_id: str,
        command_name: str,
    ) -> bool:
        _validate_key(platform, group_id, trigger_message_id, command_name)
        async with self._session_factory() as session:
            claim_id = await session.scalar(
                select(CommandTriggerClaimRecord.id).where(
                    CommandTriggerClaimRecord.platform == platform,
                    CommandTriggerClaimRecord.group_id == group_id,
                    CommandTriggerClaimRecord.trigger_message_id
                    == trigger_message_id,
                    CommandTriggerClaimRecord.command_name == command_name,
                )
            )
            return claim_id is not None


def _validate_key(
    platform: str,
    group_id: str,
    trigger_message_id: str,
    command_name: str,
) -> None:
    for name, value in (
        ("platform", platform),
        ("group_id", group_id),
        ("trigger_message_id", trigger_message_id),
        ("command_name", command_name),
    ):
        if not value:
            raise ValueError(f"{name} must not be empty")
