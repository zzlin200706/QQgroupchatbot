"""Transactional persistence and historical retrieval of validated summaries."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.summaries import StoredSummary, SummaryResult
from app.storage.models import SummaryRecord
from app.storage.summary_codec import decode_summary, encode_summary


class SummaryRepository:
    """Store each successful summary generation as an independent history row."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def persist(self, result: SummaryResult) -> StoredSummary:
        """Atomically insert one validated result without deduplication."""

        record = encode_summary(result, created_at=self._clock())
        async with self._session_factory() as session:
            async with session.begin():
                session.add(record)
                await session.flush()
                await session.refresh(record)
            return decode_summary(record)

    async def get_by_id(self, summary_id: int) -> StoredSummary | None:
        async with self._session_factory() as session:
            record = await session.get(SummaryRecord, summary_id)
            return None if record is None else decode_summary(record)

    async def list_for_group(
        self,
        *,
        platform: str,
        group_id: str,
        limit: int = 100,
    ) -> Sequence[StoredSummary]:
        """Return newest successful runs for one platform-scoped group."""

        if not platform:
            raise ValueError("platform must not be empty")
        if not group_id:
            raise ValueError("group_id must not be empty")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        async with self._session_factory() as session:
            records = (
                await session.scalars(
                    select(SummaryRecord)
                    .where(
                        SummaryRecord.platform == platform,
                        SummaryRecord.group_id == group_id,
                    )
                    .order_by(SummaryRecord.created_at.desc(), SummaryRecord.id.desc())
                    .limit(limit)
                )
            ).all()
            return tuple(decode_summary(record) for record in records)
