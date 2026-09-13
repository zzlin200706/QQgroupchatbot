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
        # `_in_flight_counter` is only updated under `_state_lock` and
        # exists so the *synchronous* `process` path can reject early when
        # the process is already saturated, instead of leaving a queued
        # task that may be cancelled before it runs.
        self._in_flight_counter = 0
        self._state_lock = asyncio.Lock()
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
            scheduled = self._try_schedule_interaction(
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

        if timeout is None:
            tasks = tuple(self._interaction_tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            return
        tasks = tuple(self._interaction_tasks)
        if not tasks:
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

        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
        tasks = tuple(self._interaction_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _try_schedule_interaction(
        self,
        *,
        interaction_name: str,
        message: InternalMessage,
    ) -> bool:
        """Reserve a slot and create a background task; return False when saturated.

        Returning ``False`` lets the caller record that the interaction was
        *recognised* but intentionally *dropped* because the process is
        already over its bound.  This is preferable to building an
        unbounded queue of coroutines that may be cancelled at shutdown
        before ever running.
        """
        if self._closed:
            return False
        try:
            task = self._task_factory(
                self._run_interaction(interaction_name, message),
                name=f"qq-official-{interaction_name}-interaction",
            )
        except RuntimeError:
            # No running loop or other scheduling failure — refuse to leak.
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
        """Acquire the bounded slot, then dispatch the interaction."""

        async with self._state_lock:
            if self._closed:
                return None
            if self._in_flight_counter >= self._max_concurrent_interactions:
                logger.warning(
                    "qq official interaction dropped due to saturation interaction_name=%s in_flight=%s max=%s",
                    interaction_name,
                    self._in_flight_counter,
                    self._max_concurrent_interactions,
                )
                return None
            self._in_flight_counter += 1
        try:
            return await self._interaction_dispatcher.handle(message)
        except asyncio.CancelledError:
            raise
        finally:
            async with self._state_lock:
                self._in_flight_counter = max(0, self._in_flight_counter - 1)

    def _command_task_finished(self, task: asyncio.Task[object]) -> None:
        self._interaction_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            # Include the traceback in the log so post-mortem debugging
            # does not lose the original failure cause.
            logger.exception(
                "qq official interaction task failed task_name=%s error_type=%s",
                task.get_name(),
                type(error).__name__,
            )
