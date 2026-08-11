"""Shared QQ Official inbound event processing across transports."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

from app.adapters.qq_official.inbound import QQOfficialInboundEvent
from app.domain.messages import InternalMessage
from app.services.command_dispatch import QQOfficialCommandDispatcher
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
    command_name: str | None


class QQOfficialEventProcessor:
    """Persist raw payloads first, then normalize, then dispatch commands."""

    def __init__(
        self,
        *,
        raw_ingestion_service: QQOfficialRawEventIngestionService,
        normalized_ingestion_service: QQOfficialNormalizedMessageIngestionService,
        command_dispatcher: QQOfficialCommandDispatcher,
        task_factory: _TaskFactory = asyncio.create_task,
    ) -> None:
        self._raw_ingestion_service = raw_ingestion_service
        self._normalized_ingestion_service = normalized_ingestion_service
        self._command_dispatcher = command_dispatcher
        self._task_factory = task_factory
        self._command_tasks: set[asyncio.Task[object]] = set()

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
                command_name=None,
            )

        normalized = await self._normalized_ingestion_service.ingest(raw_event)
        if normalized is None:
            return QQOfficialEventProcessResult(
                transport=event.transport,
                event_type=event.event_type,
                raw_persisted=True,
                normalized_persisted=False,
                raw_event_id=raw_event.id,
                command_name=None,
            )

        command_name = self._command_dispatcher.command_name(normalized)
        if command_name is not None:
            self._schedule_command(command_name=command_name, message=normalized)

        return QQOfficialEventProcessResult(
            transport=event.transport,
            event_type=event.event_type,
            raw_persisted=True,
            normalized_persisted=True,
            raw_event_id=raw_event.id,
            command_name=command_name,
        )

    async def drain(self) -> None:
        tasks = tuple(self._command_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        tasks = tuple(self._command_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_command(
        self,
        *,
        command_name: str,
        message: InternalMessage,
    ) -> None:
        task = self._task_factory(
            self._command_dispatcher.handle(message),
            name=f"qq-official-{command_name}-command",
        )
        self._command_tasks.add(task)
        task.add_done_callback(self._command_task_finished)

    def _command_task_finished(self, task: asyncio.Task[object]) -> None:
        self._command_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "qq official command task failed task_name=%s error_type=%s",
                task.get_name(),
                type(error).__name__,
            )
