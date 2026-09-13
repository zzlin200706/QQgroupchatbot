"""Shared QQ Official inbound event processing across transports."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from app.adapters.qq_official.inbound import QQOfficialInboundEvent
from app.domain.messages import InternalMessage
from app.services.interaction_dispatch import QQOfficialInteractionDispatcher
from app.services.normalized_message_ingestion import (
    QQOfficialNormalizedMessageIngestionService,
)
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService


logger = logging.getLogger(__name__)


class _TaskFactory(Protocol):
    def __call__(
        self,
        coro: Coroutine[Any, Any, object],
        *,
        name: str | None = None,
    ) -> asyncio.Task[object]: ...


@dataclass(frozen=True)
class QQOfficialEventProcessResult:
    transport: str
    event_type: str | None
    raw_persisted: bool
    normalized_persisted: bool
    raw_event_id: int | None
    interaction_name: str | None
    interaction_scheduled: bool

    @property
    def command_name(self) -> str | None:
        """Backward-compatible alias for historical diagnostics."""

        return self.interaction_name


class QQOfficialEventProcessor:
    """Persist raw payloads first, then normalize, then dispatch commands.

    A bounded :class:`asyncio.Semaphore` keeps the number of in-flight
    LLM-bound interactions (summary / chat / QA) under control so a flood
    of group messages cannot fan out into an unbounded number of provider
    calls.

    Slot acquisition happens *synchronously* during ``process``: when the
    process is already at the bound, ``process`` records the interaction
    as recognised but not scheduled (``interaction_scheduled=False``).
    This is preferable to building an unbounded queue of coroutines that
    may be cancelled at shutdown before ever running.
    """

    def __init__(
        self,
        *,
        raw_ingestion_service: QQOfficialRawEventIngestionService,
        normalized_ingestion_service: QQOfficialNormalizedMessageIngestionService,
        interaction_dispatcher: QQOfficialInteractionDispatcher,
        task_factory: _TaskFactory = asyncio.create_task,
        max_concurrent_interactions: int = 4,
    ) -> None:
        if max_concurrent_interactions < 1:
            raise ValueError("max_concurrent_interactions must be at least one")
        self._raw_ingestion_service = raw_ingestion_service
        self._normalized_ingestion_service = normalized_ingestion_service
        self._interaction_dispatcher = interaction_dispatcher
        self._task_factory = task_factory
        self._max_concurrent_interactions = max_concurrent_interactions
        self._interaction_tasks: set[asyncio.Task[object]] = set()
        self._semaphore = asyncio.BoundedSemaphore(max_concurrent_interactions)
        self._closed = False

    async def process(
        self,
        event: QQOfficialInboundEvent,
    ) -> QQOfficialEventProcessResult:
        raw_event = await self._raw_ingestion_service.ingest(event)
        if raw_event is None:
            return QQOfficialEventProcessResult(
                transport=event.transport,
                event_type=event.event_type,
                raw_persisted=False,
                normalized_persisted=False,
                raw_event_id=None,
                interaction_name=None,
                interaction_scheduled=False,
            )

        normalized = await self._normalized_ingestion_service.ingest(raw_event)
        if normalized is None:
            return QQOfficialEventProcessResult(
                transport=event.transport,
                event_type=event.event_type,
                raw_persisted=True,
                normalized_persisted=False,
                raw_event_id=raw_event.id,
                interaction_name=None,
                interaction_scheduled=False,
            )

        interaction_name = self._interaction_dispatcher.interaction_name(normalized)
        scheduled = False
        if interaction_name is not None:
            scheduled = await self._try_schedule_interaction(
                interaction_name=interaction_name,
                message=normalized,
            )

        return QQOfficialEventProcessResult(
            transport=event.transport,
            event_type=event.event_type,
            raw_persisted=True,
            normalized_persisted=True,
            raw_event_id=raw_event.id,
            interaction_name=interaction_name,
            interaction_scheduled=scheduled,
        )

    async def drain(self, *, timeout: float | None = None) -> None:
        """Wait until all currently-scheduled interaction tasks settle."""

        tasks = tuple(self._interaction_tasks)
        if not tasks:
            return
        if timeout is None:
            await asyncio.gather(*tasks, return_exceptions=True)
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "qq official event drain timed out pending_tasks=%s",
                len(tasks),
            )

    async def aclose(self) -> None:
        """Cancel in-flight interaction tasks and stop accepting new work."""

        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._interaction_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _try_schedule_interaction(
        self,
        *,
        interaction_name: str,
        message: InternalMessage,
    ) -> bool:
        """Reserve a slot and create a background task; return False when saturated."""

        if self._closed:
            return False
        # BoundedSemaphore raises ValueError on release mismatch but
        # `acquire()` itself only returns False when no slot is free.
        acquired = self._semaphore.locked() is False and self._semaphore._value > 0  # type: ignore[attr-defined]
        if not acquired:
            logger.warning(
                "qq official interaction dropped due to saturation interaction_name=%s max=%s",
                interaction_name,
                self._max_concurrent_interactions,
            )
            return False
        try:
            task = self._task_factory(
                self._run_interaction(interaction_name, message),
                name=f"qq-official-{interaction_name}-interaction",
            )
        except RuntimeError:
            logger.warning(
                "qq official interaction scheduling refused interaction_name=%s",
                interaction_name,
            )
            return False
        self._interaction_tasks.add(task)
        task.add_done_callback(self._command_task_finished)
        return True

    async def _run_interaction(
        self,
        interaction_name: str,
        message: InternalMessage,
    ) -> object | None:
        """Hold one slot while dispatching, release on completion or cancel."""

        try:
            await self._semaphore.acquire()
        except asyncio.CancelledError:
            raise
        try:
            return await self._interaction_dispatcher.handle(message)
        finally:
            try:
                self._semaphore.release()
            except ValueError:
                # BoundedSemaphore.release raises when called more times
                # than acquired; this would indicate a logic bug, but
                # we must not raise from the finally block.
                logger.exception(
                    "qq official semaphore over-release interaction_name=%s",
                    interaction_name,
                )

    def _command_task_finished(self, task: asyncio.Task[object]) -> None:
        self._interaction_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        # `task.exception()` returns the exception object; its
        # ``__traceback__`` is still set, so we can pass it via
        # ``exc_info`` even though we are not inside an ``except``
        # block here.
        logger.error(
            "qq official interaction task failed task_name=%s error_type=%s",
            task.get_name(),
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
