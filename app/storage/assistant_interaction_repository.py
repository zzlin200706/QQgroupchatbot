"""Persistence and idempotency for successful assistant interactions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.assistant_interactions import (
    AssistantInteraction,
    AssistantTriggerType,
    StoredAssistantInteraction,
)
from app.storage.assistant_interaction_codec import (
    decode_assistant_interaction,
    encode_assistant_interaction,
)
from app.storage.models import AssistantInteractionRecord, AssistantTriggerClaimRecord


class AssistantInteractionRepository:
    """Store delivered turns and atomically claim logical inbound triggers."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def claim_trigger(
        self,
        *,
        platform: str,
        group_id: str,
        trigger_message_id: str,
        trigger_type: AssistantTriggerType,
    ) -> bool:
        """Return true only for the first durable claim of this trigger."""

        _validate_trigger_key(platform, group_id, trigger_message_id)
        record = AssistantTriggerClaimRecord(
            platform=platform,
            group_id=group_id,
            trigger_message_id=trigger_message_id,
            trigger_type=trigger_type.value,
            claimed_at=self._clock(),
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(record)
                    await session.flush()
            return True
        except IntegrityError:
            if await self.exists_for_trigger(
                platform=platform,
                group_id=group_id,
                trigger_message_id=trigger_message_id,
                trigger_type=trigger_type,
            ):
                return False
            raise

    async def exists_for_trigger(
        self,
        *,
        platform: str,
        group_id: str,
        trigger_message_id: str,
        trigger_type: AssistantTriggerType,
    ) -> bool:
        _validate_trigger_key(platform, group_id, trigger_message_id)
        async with self._session_factory() as session:
            claim_id = await session.scalar(
                select(AssistantTriggerClaimRecord.id).where(
                    AssistantTriggerClaimRecord.platform == platform,
                    AssistantTriggerClaimRecord.group_id == group_id,
                    AssistantTriggerClaimRecord.trigger_message_id
                    == trigger_message_id,
                    AssistantTriggerClaimRecord.trigger_type == trigger_type.value,
                )
            )
            return claim_id is not None

    async def insert_successful_interaction(
        self,
        interaction: AssistantInteraction,
    ) -> StoredAssistantInteraction:
        """Persist an answer only after the outbound QQ send succeeded."""

        record = encode_assistant_interaction(
            interaction,
            created_at=self._clock(),
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(record)
                    await session.flush()
                    await session.refresh(record)
                return decode_assistant_interaction(record)
        except IntegrityError:
            existing = await self._find_for_trigger(
                platform=interaction.platform,
                group_id=interaction.group_id,
                trigger_message_id=interaction.trigger_message_id,
                trigger_type=interaction.trigger_type,
            )
            if existing is None:
                raise
            return existing

    async def list_recent_for_group(
        self,
        *,
        platform: str,
        group_id: str,
        start_time: datetime,
        before_time: datetime,
        limit: int,
    ) -> Sequence[StoredAssistantInteraction]:
        _validate_group(platform, group_id)
        _require_aware("start_time", start_time)
        _require_aware("before_time", before_time)
        if start_time >= before_time:
            raise ValueError("start_time must be earlier than before_time")
        if limit < 1:
            raise ValueError("limit must be at least one")
        async with self._session_factory() as session:
            records = list(
                (
                    await session.scalars(
                        select(AssistantInteractionRecord)
                        .where(
                            AssistantInteractionRecord.platform == platform,
                            AssistantInteractionRecord.group_id == group_id,
                            AssistantInteractionRecord.trigger_timestamp >= start_time,
                            AssistantInteractionRecord.trigger_timestamp < before_time,
                        )
                        .order_by(
                            AssistantInteractionRecord.trigger_timestamp.desc(),
                            AssistantInteractionRecord.id.desc(),
                        )
                        .limit(limit)
                    )
                ).all()
            )
        records.reverse()
        return tuple(decode_assistant_interaction(record) for record in records)

    async def find_by_response_message_id(
        self,
        *,
        platform: str,
        group_id: str,
        response_message_id: str,
    ) -> StoredAssistantInteraction | None:
        _validate_group(platform, group_id)
        if not response_message_id:
            raise ValueError("response_message_id must not be empty")
        async with self._session_factory() as session:
            record = await session.scalar(
                select(AssistantInteractionRecord)
                .where(
                    AssistantInteractionRecord.platform == platform,
                    AssistantInteractionRecord.group_id == group_id,
                    AssistantInteractionRecord.response_message_id
                    == response_message_id,
                )
                .order_by(AssistantInteractionRecord.id.desc())
                .limit(1)
            )
            return None if record is None else decode_assistant_interaction(record)

    async def _find_for_trigger(
        self,
        *,
        platform: str,
        group_id: str,
        trigger_message_id: str,
        trigger_type: AssistantTriggerType,
    ) -> StoredAssistantInteraction | None:
        async with self._session_factory() as session:
            record = await session.scalar(
                select(AssistantInteractionRecord).where(
                    AssistantInteractionRecord.platform == platform,
                    AssistantInteractionRecord.group_id == group_id,
                    AssistantInteractionRecord.trigger_message_id
                    == trigger_message_id,
                    AssistantInteractionRecord.trigger_type == trigger_type.value,
                )
            )
            return None if record is None else decode_assistant_interaction(record)


def _validate_trigger_key(
    platform: str,
    group_id: str,
    trigger_message_id: str,
) -> None:
    _validate_group(platform, group_id)
    if not trigger_message_id:
        raise ValueError("trigger_message_id must not be empty")


def _validate_group(platform: str, group_id: str) -> None:
    if not platform:
        raise ValueError("platform must not be empty")
    if not group_id:
        raise ValueError("group_id must not be empty")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
