"""Repository operations for the durable interaction outbox."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import InteractionOutboxRecord, RawEvent


logger = logging.getLogger(__name__)


class InteractionOutboxRepository:
    """Persist and transition per-interaction outbox rows.

    The repository owns the state-machine transitions.  Callers do
    not write ``status`` columns directly; they ask for transitions
    such as ``claim_next_pending`` or ``mark_completed``.  This
    keeps the SQL needed to be atomic in one place and makes the
    state machine easy to test.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        # The claim path is single-process per AGENTS.md.  An in-process
        # lock is enough to keep two worker tasks from claiming the
        # same row; SQLite does not honour ``FOR UPDATE SKIP LOCKED``,
        # so a SQL-level row lock is not available.
        self._claim_lock = asyncio.Lock()

    async def create_pending(
        self,
        *,
        platform: str,
        group_id: str,
        trigger_message_id: str,
        interaction_name: str,
        source_raw_event_id: int,
        max_attempts: int = 3,
    ) -> tuple[InteractionOutboxRecord, bool]:
        """Insert a new pending row, returning (row, created).

        On ``UNIQUE`` violation (the same trigger already has an
        outbox row) the existing row is returned with ``created=False``
        so the caller can decide whether to skip the work entirely.
        """

        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")

        try:
            async with self._session_factory() as session:
                record = InteractionOutboxRecord(
                    platform=platform,
                    group_id=group_id,
                    trigger_message_id=trigger_message_id,
                    interaction_name=interaction_name,
                    source_raw_event_id=source_raw_event_id,
                    status=InteractionOutboxRecord.STATUS_PENDING,
                    attempt_count=0,
                    max_attempts=max_attempts,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(record)
                await session.commit()
                await session.refresh(record)
            return record, True
        except IntegrityError:
            existing = await self.get_by_trigger(
                platform=platform,
                group_id=group_id,
                trigger_message_id=trigger_message_id,
                interaction_name=interaction_name,
            )
            if existing is None:
                raise
            return existing, False

    async def get_by_trigger(
        self,
        *,
        platform: str,
        group_id: str,
        trigger_message_id: str,
        interaction_name: str,
    ) -> InteractionOutboxRecord | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(InteractionOutboxRecord).where(
                    InteractionOutboxRecord.platform == platform,
                    InteractionOutboxRecord.group_id == group_id,
                    InteractionOutboxRecord.trigger_message_id == trigger_message_id,
                    InteractionOutboxRecord.interaction_name == interaction_name,
                )
            )

    async def get_by_id(
        self, outbox_id: int
    ) -> InteractionOutboxRecord | None:
        async with self._session_factory() as session:
            return await session.get(InteractionOutboxRecord, outbox_id)

    async def claim_next_pending(self) -> InteractionOutboxRecord | None:
        """Atomically claim one pending row, returning it as 'running'.

        The bot is single-process (per AGENTS.md), so a process-local
        ``asyncio.Lock`` is enough to serialise claim attempts across
        worker tasks.  SQLite does not honour
        ``FOR UPDATE SKIP LOCKED`` in the way PostgreSQL does, so a
        SQL-level row lock is not available; the in-process lock is
        the documented contract.
        """

        async with self._claim_lock:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(InteractionOutboxRecord)
                        .where(
                            InteractionOutboxRecord.status == InteractionOutboxRecord.STATUS_PENDING,
                            InteractionOutboxRecord.attempt_count >= InteractionOutboxRecord.max_attempts,
                        )
                        .values(
                            status=InteractionOutboxRecord.STATUS_FAILED,
                            completed_at=datetime.now(timezone.utc),
                            last_error="retry limit reached before claim",
                        )
                    )
                    row = await session.scalar(
                        select(InteractionOutboxRecord)
                        .where(InteractionOutboxRecord.status == InteractionOutboxRecord.STATUS_PENDING)
                        .order_by(InteractionOutboxRecord.id.asc())
                        .limit(1)
                    )
                    if row is None:
                        return None
                    row.status = InteractionOutboxRecord.STATUS_RUNNING
                    row.started_at = datetime.now(timezone.utc)
                    row.attempt_count = row.attempt_count + 1
                    await session.flush()
                    await session.refresh(row)
                    return row

    async def mark_completed(
        self,
        outbox_id: int,
        *,
        response_message_id: str | None,
    ) -> InteractionOutboxRecord | None:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(InteractionOutboxRecord, outbox_id)
                if row is None or row.status != InteractionOutboxRecord.STATUS_RUNNING:
                    return None
                row.status = InteractionOutboxRecord.STATUS_COMPLETED
                row.completed_at = datetime.now(timezone.utc)
                row.response_message_id = response_message_id
                row.last_error = None
                await session.flush()
                await session.refresh(row)
                return row

    async def mark_failed(
        self,
        outbox_id: int,
        *,
        reason: str,
    ) -> InteractionOutboxRecord | None:
        """Mark a running row as permanently failed (no more retries)."""

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(InteractionOutboxRecord, outbox_id)
                if row is None or row.status != InteractionOutboxRecord.STATUS_RUNNING:
                    return None
                row.status = InteractionOutboxRecord.STATUS_FAILED
                row.completed_at = datetime.now(timezone.utc)
                row.last_error = reason[:1024]
                await session.flush()
                await session.refresh(row)
                return row

    async def requeue_for_retry(
        self,
        outbox_id: int,
        *,
        reason: str,
    ) -> InteractionOutboxRecord | None:
        """Send a running row back to pending so the next worker picks it up.

        The caller has already incremented ``attempt_count`` when it
        claimed the row.  If ``attempt_count >= max_attempts`` the
        row is moved to ``failed`` instead.
        """

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(InteractionOutboxRecord, outbox_id)
                if row is None or row.status != InteractionOutboxRecord.STATUS_RUNNING:
                    return None
                if row.attempt_count >= row.max_attempts:
                    row.status = InteractionOutboxRecord.STATUS_FAILED
                    row.completed_at = datetime.now(timezone.utc)
                    row.last_error = (
                        f"retry limit reached: {reason[:1024]}"
                    )
                else:
                    row.status = InteractionOutboxRecord.STATUS_PENDING
                    row.started_at = None
                    row.last_error = reason[:1024]
                await session.flush()
                await session.refresh(row)
                return row

    async def list_recoverable(self) -> Sequence[InteractionOutboxRecord]:
        """Return rows that need to be re-evaluated on startup.

        ``pending`` rows are simply waiting for a worker.
        ``running`` rows are stale; they belonged to a process that
        no longer exists.  ``completed`` and ``failed`` rows are
        final and never returned.
        """

        async with self._session_factory() as session:
            result = await session.scalars(
                select(InteractionOutboxRecord).where(
                    InteractionOutboxRecord.status.in_(
                        [
                            InteractionOutboxRecord.STATUS_PENDING,
                            InteractionOutboxRecord.STATUS_RUNNING,
                        ]
                    )
                )
            )
            return result.all()

    async def recover_stale_running(self) -> int:
        """Recover stale ``running`` rows without exceeding the attempt limit.

        A row is considered stale when it is still ``running``; on
        startup the previous owner cannot be running any more.  The
        transition keeps ``attempt_count`` as-is so the row is
        still subject to the per-row max-attempts bound.
        """

        async with self._session_factory() as session:
            async with session.begin():
                exhausted = await session.execute(
                    update(InteractionOutboxRecord)
                    .where(
                        InteractionOutboxRecord.status == InteractionOutboxRecord.STATUS_RUNNING,
                        InteractionOutboxRecord.attempt_count >= InteractionOutboxRecord.max_attempts,
                    )
                    .values(
                        status=InteractionOutboxRecord.STATUS_FAILED,
                        completed_at=datetime.now(timezone.utc),
                        last_error="retry limit reached during recovery; delivery unconfirmed",
                    )
                )
                result = await session.execute(
                    update(InteractionOutboxRecord)
                    .where(
                        InteractionOutboxRecord.status
                        == InteractionOutboxRecord.STATUS_RUNNING
                    )
                    .values(
                        status=InteractionOutboxRecord.STATUS_PENDING,
                        started_at=None,
                    )
                )
                return int(result.rowcount or 0) + int(exhausted.rowcount or 0)

    async def raw_event_for(
        self, outbox: InteractionOutboxRecord
    ) -> RawEvent | None:
        async with self._session_factory() as session:
            return await session.get(RawEvent, outbox.source_raw_event_id)

    async def stats(self) -> dict[str, int]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    InteractionOutboxRecord.status,
                ).order_by(InteractionOutboxRecord.id.asc())
            )
            counts: dict[str, int] = {status: 0 for status in InteractionOutboxRecord._VALID_STATUSES}
            for (status,) in result.all():
                counts[status] = counts.get(status, 0) + 1
            return counts

    async def prune_terminal(
        self,
        *,
        older_than: datetime,
    ) -> int:
        """Delete 'completed' / 'failed' rows older than ``older_than``.

        Pending and running rows are never deleted; they are still
        needed for the next start to recover.  Returns the number of
        rows deleted.
        """

        from sqlalchemy import delete

        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    delete(InteractionOutboxRecord).where(
                        InteractionOutboxRecord.status.in_(
                            [
                                InteractionOutboxRecord.STATUS_COMPLETED,
                                InteractionOutboxRecord.STATUS_FAILED,
                            ]
                        ),
                        InteractionOutboxRecord.completed_at.is_not(None),
                        InteractionOutboxRecord.completed_at < older_than,
                    )
                )
                return int(result.rowcount or 0)

    @staticmethod
    def as_dict(record: InteractionOutboxRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "platform": record.platform,
            "group_id": record.group_id,
            "trigger_message_id": record.trigger_message_id,
            "interaction_name": record.interaction_name,
            "source_raw_event_id": record.source_raw_event_id,
            "status": record.status,
            "attempt_count": record.attempt_count,
            "max_attempts": record.max_attempts,
            "response_message_id": record.response_message_id,
            "last_error": record.last_error,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
        }
