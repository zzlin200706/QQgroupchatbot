"""Drain the interaction outbox in a bounded worker pool.

The worker is the consumer side of the durable outbox.  It:

  * claims one ``pending`` row at a time per worker, atomically
    setting it to ``running`` (so concurrent workers never race for
    the same row);
  * decodes the trigger message from ``raw_events`` and the
    normalized ``messages`` table;
  * runs the existing ``QQOfficialInteractionDispatcher.handle``;
  * on success, records the QQ response and transitions the outbox
    row to ``completed``;
  * on a retryable failure, transitions to ``pending`` (or to
    ``failed`` if ``attempt_count >= max_attempts``);
  * on ``asyncio.CancelledError`` leaves the row in ``running`` so a
    restart can recover it.

The actual execution of the LLM-bound work is funnelled through a
single :class:`asyncio.Semaphore` sized by
``llm_max_concurrent_interactions``.  This is the same bound the
audit already established; the worker pool does not multiply it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from app.domain.messages import InternalMessage
from app.storage.assistant_interaction_repository import AssistantInteractionRepository
from app.storage.interaction_outbox_repository import InteractionOutboxRepository
from app.storage.message_codec import decode_message
from app.storage.models import InteractionOutboxRecord, MessageNodeRecord, MessageRecord
from app.storage.raw_event_repository import RawEventRepository  # noqa: F401
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


logger = logging.getLogger(__name__)


HandleCallable = Callable[[InternalMessage], Awaitable[object]]


class InteractionOutboxWorker:
    """A bounded worker that drains the interaction outbox."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        outbox_repository: InteractionOutboxRepository,
        raw_event_repository: RawEventRepository,
        interaction_repository: AssistantInteractionRepository | None,
        on_handle: HandleCallable,
        max_concurrent: int = 4,
        num_workers: int = 1,
        poll_interval_seconds: float = 0.5,
        recover_on_start: bool = True,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least one")
        if num_workers < 1:
            raise ValueError("num_workers must be at least one")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._session_factory = session_factory
        self._outbox_repository = outbox_repository
        self._raw_event_repository = raw_event_repository
        self._interaction_repository = interaction_repository
        self._on_handle = on_handle
        self._num_workers = num_workers
        self._poll_interval_seconds = poll_interval_seconds
        self._recover_on_start = recover_on_start
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._started = False

    async def recover(self) -> int:
        """Move stale ``running`` rows back to ``pending`` at startup."""

        if not self._recover_on_start:
            return 0
        recovered = await self._outbox_repository.recover_stale_running()
        if recovered:
            logger.warning(
                "qq official outbox recovered stale running rows count=%s",
                recovered,
            )
        return recovered

    async def prune_terminal(self, retention_days: int) -> int:
        """Delete terminal (completed / failed) outbox rows older than the retention.

        This bounds the size of the outbox table on long-running
        installations.  The operation is idempotent and safe to call
        from a background timer or from the lifespan.
        """

        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        if retention_days == 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted = await self._outbox_repository.prune_terminal(older_than=cutoff)
        if deleted:
            logger.info(
                "qq official outbox pruned terminal rows count=%s retention_days=%s",
                deleted,
                retention_days,
            )
        return deleted

    async def prune_raw_events(
        self, retention_days: int, *, chunk_size: int = 1000
    ) -> int:
        """Prune ``raw_events`` older than ``retention_days`` safely.

        The operation is idempotent and FK-safe: a row referenced by
        any ``messages`` row or by any pending/running
        ``interaction_outbox`` row is never deleted, even if it
        would otherwise be too old.
        """

        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        if retention_days == 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted = await self._raw_event_repository.prune_older_than(
            older_than=cutoff, chunk_size=chunk_size
        )
        if deleted:
            logger.info(
                "qq official raw_events pruned count=%s retention_days=%s",
                deleted,
                retention_days,
            )
        return deleted

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self.recover()
        for index in range(self._num_workers):
            task = asyncio.create_task(
                self._worker_loop(index), name=f"qq-official-outbox-{index}"
            )
            self._tasks.add(task)

    async def stop(self) -> None:
        self._stop.set()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False

    async def _worker_loop(self, index: int) -> None:
        del index
        while not self._stop.is_set():
            claimed = await self._outbox_repository.claim_next_pending()
            if claimed is None:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._poll_interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._run_claimed_row(claimed)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # The inner call already attempted requeue/mark-failed
                # where appropriate; this is a defence-in-depth catch
                # so a bug does not kill the worker loop.
                logger.exception(
                    "qq official outbox worker caught unhandled error outbox_id=%s",
                    claimed.id,
                )
                await self._outbox_repository.requeue_for_retry(
                    claimed.id,
                    reason=f"worker loop unhandled: {type(error).__name__}: {error}",
                )

    async def _run_claimed_row(self, claimed: InteractionOutboxRecord) -> None:
        outbox_id = claimed.id
        interaction_name = claimed.interaction_name
        message = await self._load_message(claimed)
        if message is None:
            await self._outbox_repository.mark_failed(
                outbox_id,
                reason="message could not be reloaded from raw_events",
            )
            return

        try:
            await self._semaphore.acquire()
        except asyncio.CancelledError:
            # Leave the row in 'running'; the next restart recovers it.
            raise

        try:
            try:
                await self._on_handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._outbox_repository.requeue_for_retry(
                    outbox_id,
                    reason=f"handler failed: {type(error).__name__}: {error}",
                )
                return
        finally:
            try:
                self._semaphore.release()
            except ValueError:
                logger.exception(
                    "qq official outbox semaphore over-release outbox_id=%s",
                    outbox_id,
                )

        response_message_id = await self._response_message_id(claimed, message)
        await self._outbox_repository.mark_completed(
            outbox_id,
            response_message_id=response_message_id,
        )
        del interaction_name  # silence unused warning when logging trimmed

    async def _load_message(
        self, claimed: InteractionOutboxRecord
    ) -> InternalMessage | None:
        async with self._session_factory() as session:
            message_record = await session.scalar(
                select(MessageRecord)
                .where(MessageRecord.source_raw_event_id == claimed.source_raw_event_id)
                .order_by(MessageRecord.id.desc())
                .limit(1)
            )
            if message_record is not None:
                node_records = (
                    await session.scalars(
                        select(MessageNodeRecord).where(
                            MessageNodeRecord.message_id == message_record.id
                        )
                    )
                ).all()
                return decode_message(message_record, list(node_records))

        # Fall back: re-parse the raw payload.
        from app.parsers.qq_official_message_parser import QQOfficialMessageParser
        raw_event = await self._raw_event_repository.get_by_id(claimed.source_raw_event_id)
        if raw_event is None:
            return None
        parser = QQOfficialMessageParser()
        return parser.parse(
            raw_event.raw_payload, raw_event_id=claimed.source_raw_event_id
        )

    async def _response_message_id(
        self, claimed: InteractionOutboxRecord, message: InternalMessage
    ) -> str | None:
        if (
            claimed.interaction_name in {"grounded_qa", "mention_chat"}
            and self._interaction_repository is not None
        ):
            rows = await self._interaction_repository.list_recent_for_group(
                platform=claimed.platform,
                group_id=claimed.group_id,
                start_time=datetime(1970, 1, 1, tzinfo=timezone.utc),
                before_time=datetime.now(timezone.utc),
                limit=1,
            )
            if rows:
                return rows[0].interaction.response_message_id
        return None
