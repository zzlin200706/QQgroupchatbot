"""Per-group AI configuration overrides.

The repository owns the read/write surface for ``group_ai_configs``;
all callers (including the resolver and any future admin API) must
go through this module so that the validation contract is enforced
in exactly one place.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import GroupAIConfigRecord


class GroupAIConfigRepository:
    """Persist and retrieve per-group AI configuration overrides.

    The ``upsert`` method is the only write path: the ``(platform,
    group_id)`` pair is unique, and the caller may set either knob
    (or both).  Passing ``None`` for a field clears that knob back
    to the global default; omitting the keyword (in keyword form)
    is equivalent to passing ``None`` because the field is
    nullable and the operator has no useful distinction between
    "leave it alone" and "set it to NULL" in this model — both
    mean "fall back to the global default".
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def get(
        self, platform: str, group_id: str
    ) -> GroupAIConfigRecord | None:
        """Return the override row for one (platform, group_id) pair, or None."""

        if not platform:
            raise ValueError("platform must not be empty")
        if not group_id:
            raise ValueError("group_id must not be empty")
        async with self._session_factory() as session:
            return await session.scalar(
                select(GroupAIConfigRecord).where(
                    GroupAIConfigRecord.platform == platform,
                    GroupAIConfigRecord.group_id == group_id,
                )
            )

    async def upsert(
        self,
        platform: str,
        group_id: str,
        *,
        enabled: bool | None = None,
        summary_max_input_chars: int | None = None,
    ) -> GroupAIConfigRecord:
        """Create or update the override row for one (platform, group_id) pair.

        Fields default to ``None`` (use the global default).  To clear
        an existing override, call with ``None`` for that field.
        """

        if not platform:
            raise ValueError("platform must not be empty")
        if not group_id:
            raise ValueError("group_id must not be empty")
        if summary_max_input_chars is not None and summary_max_input_chars < 1:
            raise ValueError(
                "summary_max_input_chars override must be a positive integer or null"
            )

        now = self._clock()
        async with self._session_factory() as session:
            async with session.begin():
                record = await session.scalar(
                    select(GroupAIConfigRecord).where(
                        GroupAIConfigRecord.platform == platform,
                        GroupAIConfigRecord.group_id == group_id,
                    )
                )
                if record is None:
                    record = GroupAIConfigRecord(
                        platform=platform,
                        group_id=group_id,
                        enabled=enabled,
                        summary_max_input_chars=summary_max_input_chars,
                        updated_at=now,
                    )
                    session.add(record)
                else:
                    record.enabled = enabled
                    record.summary_max_input_chars = summary_max_input_chars
                    record.updated_at = now
                await session.flush()
                await session.refresh(record)
                return record
