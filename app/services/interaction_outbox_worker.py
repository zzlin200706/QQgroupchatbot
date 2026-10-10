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
  * records handler failures as ``failed`` without replaying an
    operation whose outbound delivery may be unknown;
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

from app.domain.assistant_interactions import (
    AssistantTriggerType,
    StoredAssistantInteraction,
)
from app.domain.messages import InternalMessage
from app.parsers import QQOfficialMessageParser
from app.services.command_status import CommandStatus
from app.services.group_assistant_handler import GroupAssistantStatus
from app.storage.assistant_interaction_repository import AssistantInteractionRepository
from app.storage.database import Database
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
        message_parser: QQOfficialMessageParser | None = None,
        database: "Database | None" = None,
        max_concurrent: int = 4,
        num_workers: int = 1,
        poll_interval_seconds: float = 0.5,
        recover_on_start: bool = True,
        clock: Callable[[], datetime] | None = None,
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
        self._message_parser = message_parser or QQOfficialMessageParser()
        self._database = database
        self._num_workers = num_workers
        self._poll_interval_seconds = poll_interval_seconds
        self._recover_on_start = recover_on_start
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._started = False

    @property
    def is_running(self) -> bool:
        return (
            self._started
            and not self._stop.is_set()
            and len(self._tasks) == self._num_workers
            and all(not task.done() for task in self._tasks)
        )

    async def recover(self) -> int:
        """Move stale ``running`` rows back to ``pending`` at startup."""

        if not self._recover_on_start:
            return 0
        recovered = await self._outbox_repository.recover_stale_running()
        if recovered:
            from app.observability import event
            event("outbox.recovered", count=recovered)
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
            from app.observability import event
            event("outbox.pruned_terminal", count=deleted, retention_days=retention_days)
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
            from app.observability import event
            event("outbox.pruned_raw_events", count=deleted, retention_days=retention_days)
            logger.info(
                "qq official raw_events pruned count=%s retention_days=%s",
                deleted,
                retention_days,
            )
        return deleted

    async def start(self) -> None:
        if self._started:
            return
        self._stop.clear()
        # Force a WAL checkpoint so the worker starts against a
        # consistent database file.  Cheap on a quiet system and
        # bounds the WAL size on a busy one.
        if self._database is not None:
            try:
                await self._database.checkpoint_wal()
            except Exception:
                logger.exception("qq official outbox startup checkpoint failed")
        await self.recover()
        for index in range(self._num_workers):
            task = asyncio.create_task(
                self._worker_loop(index), name=f"qq-official-outbox-{index}"
            )
            self._tasks.add(task)
        self._started = True
        from app.observability import event
        event("worker.started", num_workers=self._num_workers, max_concurrent=self._semaphore._value)  # type: ignore[attr-defined]

    async def stop(self, *, drain_timeout_seconds: float = 0.0) -> None:
        """Stop claiming work, allow a bounded drain, then cancel leftovers."""

        if drain_timeout_seconds < 0:
            raise ValueError("drain_timeout_seconds must be non-negative")
        self._stop.set()
        tasks = tuple(self._tasks)
        try:
            if tasks and drain_timeout_seconds:
                await asyncio.wait(tasks, timeout=drain_timeout_seconds)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
            self._started = False
        from app.observability import event
        event("worker.stopped", num_workers=self._num_workers)

    async def _worker_loop(self, index: int) -> None:
        del index
        while not self._stop.is_set():
            try:
                claimed = await self._outbox_repository.claim_next_pending()
            except Exception as error:
                logger.warning(
                    "qq official outbox claim failed error_type=%s",
                    type(error).__name__,
                )
                await self._wait_for_work()
                continue
            if claimed is None:
                await self._wait_for_work()
                continue
            try:
                await self._run_claimed_row(claimed)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Delivery or its persistence may already have happened.
                # Keep 'running' for startup reconciliation, never replay
                # the handler merely because recording its outcome failed.
                logger.warning(
                    "qq official outbox outcome unconfirmed outbox_id=%s error_type=%s",
                    claimed.id,
                    type(error).__name__,
                    exc_info=error,
                )
                await self._wait_for_work()

    async def _wait_for_work(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop.wait(), timeout=self._poll_interval_seconds
            )
        except asyncio.TimeoutError:
            pass

    async def _run_claimed_row(self, claimed: InteractionOutboxRecord) -> None:
        outbox_id = claimed.id
        interaction_name = claimed.interaction_name
        try:
            message = await self._load_message(claimed)
        except Exception as error:
            # No handler has run, so retrying only this read is safe.
            await self._outbox_repository.requeue_for_retry(
                outbox_id, reason=f"message reload failed: {type(error).__name__}"
            )
            await self._wait_for_work()
            return
        if message is None:
            await self._outbox_repository.mark_failed(
                outbox_id,
                reason="message could not be reloaded from raw_events",
            )
            from app.observability import event
            event("outbox.failed", outbox_id=outbox_id, reason="message_reload_failed")
            return

        async with self._semaphore:
            delivered = await self._delivered_interaction(claimed)
            if delivered is not None:
                await self._outbox_repository.mark_completed(
                    outbox_id,
                    response_message_id=delivered.interaction.response_message_id,
                )
                return
            timestamp = message.timestamp
            if timestamp is None or timestamp.utcoffset() is None:
                await self._outbox_repository.mark_failed(
                    outbox_id, reason="trigger timestamp unavailable"
                )
                return
            if self._clock() - timestamp >= timedelta(minutes=5):
                await self._outbox_repository.mark_failed(
                    outbox_id, reason="passive reply window expired before execution"
                )
                return
            try:
                outcome = await self._on_handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._outbox_repository.mark_failed(
                    outbox_id,
                    reason=f"handler raised {type(error).__name__}; delivery unconfirmed; automatic retry disabled",
                )
                return

            if not isinstance(outcome, (CommandStatus, GroupAssistantStatus)):
                reason = "handler returned no recognized outcome; delivery unconfirmed"
            elif outcome.value == "duplicate":
                reason = "trigger already claimed without confirmed delivery; automatic retry disabled"
            elif outcome.value not in {"succeeded", "usage_sent"}:
                reason = f"handler returned {outcome.value}; automatic retry disabled"
            else:
                reason = None
            if reason is not None:
                await self._outbox_repository.mark_failed(
                    outbox_id, reason=reason
                )
                return

        delivered = await self._delivered_interaction(claimed)
        response_message_id = (
            None if delivered is None else delivered.interaction.response_message_id
        )
        await self._outbox_repository.mark_completed(
            outbox_id,
            response_message_id=response_message_id,
        )
        from app.observability import event
        event(
            "outbox.completed",
            outbox_id=outbox_id,
            interaction=interaction_name,
            attempts=claimed.attempt_count,
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
        raw_event = await self._raw_event_repository.get_by_id(claimed.source_raw_event_id)
        if raw_event is None:
            return None
        return self._message_parser.parse(
            raw_event.raw_payload, raw_event_id=claimed.source_raw_event_id
        )

    async def _delivered_interaction(
        self, claimed: InteractionOutboxRecord
    ) -> StoredAssistantInteraction | None:
        if (
            claimed.interaction_name in {trigger.value for trigger in AssistantTriggerType}
            and self._interaction_repository is not None
        ):
            return await self._interaction_repository.find_for_trigger(
                platform=claimed.platform,
                group_id=claimed.group_id,
                trigger_message_id=claimed.trigger_message_id,
                trigger_type=AssistantTriggerType(claimed.interaction_name),
            )
        return None
