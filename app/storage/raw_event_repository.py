"""Repository operations for raw event receipts."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import (
    InteractionOutboxRecord,
    MessageRecord,
    RawEvent,
)


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

    async def prune_older_than(
        self,
        *,
        older_than: datetime,
        chunk_size: int = 1000,
    ) -> int:
        """Delete raw_events older than ``older_than`` that are safe to drop.

        A raw_event is safe to drop when *all* of the following hold:

          * ``received_at`` is strictly before ``older_than``;
          * it is not referenced by any row in ``messages``
            (the parsed message is the source of truth for context);
          * it is not referenced by any ``interaction_outbox`` row in
            ``pending`` or ``running`` state (the worker re-parses
            the raw payload during crash recovery).

        The FK constraints (``ON DELETE RESTRICT``) are the safety net:
        if any of the above checks are wrong, the database refuses the
        delete.  The chunked ``LIMIT`` keeps the working set small
        on long-running bots that may have grown the table to
        millions of rows.

        To make the operation safe in the presence of completed /
        failed outbox rows (which also hold an FK to the raw_event),
        the prune also deletes any such outbox rows that reference a
        row being pruned.  Pending / running rows are never deleted
        and therefore continue to block the prune.

        Returns the number of raw_events actually deleted.
        """

        if chunk_size < 1:
            raise ValueError("chunk_size must be at least one")

        total_deleted = 0
        while True:
            async with self._session_factory() as session:
                async with session.begin():
                    # Build the candidate-id set in a single statement
                    # and let SQLAlchemy turn the ``in_`` into a NOT
                    # EXISTS sub-query.  This is portable and avoids
                    # driver-specific IN-list limits.
                    candidate_ids_subquery = (
                        select(RawEvent.id)
                        .where(
                            RawEvent.received_at < older_than,
                            ~select(MessageRecord.id)
                            .where(MessageRecord.source_raw_event_id == RawEvent.id)
                            .exists(),
                            ~(
                                select(InteractionOutboxRecord.id)
                                .where(
                                    InteractionOutboxRecord.source_raw_event_id
                                    == RawEvent.id,
                                    InteractionOutboxRecord.status.in_(
                                        [
                                            InteractionOutboxRecord.STATUS_PENDING,
                                            InteractionOutboxRecord.STATUS_RUNNING,
                                        ]
                                    ),
                                )
                                .exists()
                            ),
                        )
                        .order_by(RawEvent.id.asc())
                        .limit(chunk_size)
                        .subquery()
                    )
                    # First, drop any terminal outbox rows that
                    # reference the raw_events we are about to prune.
                    # This releases the FK so the raw_events delete
                    # below does not hit ``FOREIGN KEY constraint
                    # failed``.  Pending and running rows are not
                    # touched, so a still-in-flight interaction
                    # still blocks the prune.
                    await session.execute(
                        delete(InteractionOutboxRecord).where(
                            InteractionOutboxRecord.source_raw_event_id.in_(
                                select(candidate_ids_subquery.c.id)
                            ),
                            InteractionOutboxRecord.status.in_(
                                [
                                    InteractionOutboxRecord.STATUS_COMPLETED,
                                    InteractionOutboxRecord.STATUS_FAILED,
                                ]
                            ),
                        )
                    )
                    result = await session.execute(
                        delete(RawEvent).where(
                            RawEvent.id.in_(select(candidate_ids_subquery.c.id))
                        )
                    )
                    deleted = int(result.rowcount or 0)
            total_deleted += deleted
            if deleted < chunk_size:
                return total_deleted
