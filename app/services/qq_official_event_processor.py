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

    @property
    def command_name(self) -> str | None:
        """Backward-compatible alias for Phase M diagnostics."""

        return self.interaction_name


class QQOfficialEventProcessor:
    """Persist raw payloads first, then normalize, then dispatch commands."""

    def __init__(
        self,
        *,
        raw_ingestion_service: QQOfficialRawEventIngestionService,
        normalized_ingestion_service: QQOfficialNormalizedMessageIngestionService,
        interaction_dispatcher: QQOfficialInteractionDispatcher,
        task_factory: _TaskFactory = asyncio.create_task,
    ) -> None:
        self._raw_ingestion_service = raw_ingestion_service
        self._normalized_ingestion_service = normalized_ingestion_service
        self._interaction_dispatcher = interaction_dispatcher
        self._task_factory = task_factory
        self._interaction_tasks: set[asyncio.Task[object]] = set()

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
            )

        interaction_name = self._interaction_dispatcher.interaction_name(normalized)
        if interaction_name is not None:
            self._schedule_interaction(
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
        )

    async def drain(self) -> None:
        tasks = tuple(self._interaction_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        tasks = tuple(self._interaction_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_interaction(
        self,
        *,
        interaction_name: str,
        message: InternalMessage,
    ) -> None:
        task = self._task_factory(
            self._interaction_dispatcher.handle(message),
            name=f"qq-official-{interaction_name}-interaction",
        )
        self._interaction_tasks.add(task)
        task.add_done_callback(self._command_task_finished)

    def _command_task_finished(self, task: asyncio.Task[object]) -> None:
        self._interaction_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "qq official interaction task failed task_name=%s error_type=%s",
                task.get_name(),
                type(error).__name__,
            )
