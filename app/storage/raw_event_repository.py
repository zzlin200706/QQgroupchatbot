"""Repository operations for raw event receipts."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import RawEvent


logger = logging.getLogger(__name__)


class RawEventRepository:
    """Persist and retrieve raw event receipts with one session per operation."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert(self, event: RawEvent) -> RawEvent:
        """Insert one receipt in its own transaction.

        If an identical receipt already exists (same ``platform`` and
        ``payload_hash``) the existing row is returned instead of
        raising.  This keeps the raw-first principle intact: a duplicate
        Gateway dispatch or webhook retry never produces two raw rows
        and never causes the caller to fail.
        """

        try:
            async with self._session_factory() as session:
                try:
                    session.add(event)
                    await session.commit()
                    await session.refresh(event)
                except Exception:
                    await session.rollback()
                    raise
            return event
        except IntegrityError:
            existing = await self.get_by_payload_hash(
                event.platform, event.payload_hash
            )
            if existing is None:
                raise
            return existing

    async def get_by_id(self, raw_event_id: int) -> RawEvent | None:
        """Return one event receipt by its local primary key."""

        async with self._session_factory() as session:
            return await session.get(RawEvent, raw_event_id)

    async def get_by_payload_hash(
        self, platform: str, payload_hash: str
    ) -> RawEvent | None:
        """Return one event receipt by (platform, payload_hash)."""

        async with self._session_factory() as session:
            return await session.scalar(
                select(RawEvent).where(
                    RawEvent.platform == platform,
                    RawEvent.payload_hash == payload_hash,
                )
            )

    async def list_recent(self, limit: int = 100) -> Sequence[RawEvent]:
        """Return the newest receipts, without interpreting their payloads."""

        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        async with self._session_factory() as session:
            result = await session.scalars(
                select(RawEvent).order_by(RawEvent.id.desc()).limit(limit)
            )
            return result.all()
