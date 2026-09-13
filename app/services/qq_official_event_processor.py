"""Shared QQ Official inbound event processing across transports.

The processor is the *producer* side of the durable outbox pipeline.
It writes a ``pending`` row for every recognised interaction; the
:class:`InteractionOutboxWorker` is the consumer that actually runs
the LLM-bound work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.adapters.qq_official.inbound import QQOfficialInboundEvent
from app.services.interaction_dispatch import QQOfficialInteractionDispatcher
from app.services.normalized_message_ingestion import (
    QQOfficialNormalizedMessageIngestionService,
)
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.storage.interaction_outbox_repository import InteractionOutboxRepository


logger = logging.getLogger(__name__)


# The QQ Official adapter is the only supported platform at the
# moment (per AGENTS.md).  When a second adapter is added, the
# platform value should be derived from the inbound event instead
# of being a constant.
QQ_OFFICIAL_PLATFORM = "qq_official"


@dataclass(frozen=True)
class QQOfficialEventProcessResult:
    transport: str
    event_type: str | None
    raw_persisted: bool
    normalized_persisted: bool
    raw_event_id: int | None
    interaction_name: str | None
    outbox_id: int | None = None
    outbox_already_existed: bool = False

    @property
    def command_name(self) -> str | None:
        """Backward-compatible alias for historical diagnostics."""

        return self.interaction_name

    @property
    def interaction_scheduled(self) -> bool:
        """Backward-compatible alias for historical diagnostics.

        Always ``True`` for newly-created outbox rows.  Always
        ``False`` for a row we refused to create (duplicate or
        missing identifier).
        """

        return self.outbox_id is not None and not self.outbox_already_existed


class QQOfficialEventProcessor:
    """Persist raw payloads first, then normalize, then enqueue work.

    Every recognised interaction is recorded in a durable outbox so
    a SIGTERM or crash does not silently lose the reply: the
    :class:`InteractionOutboxWorker` re-queues any ``pending`` or
    stale ``running`` rows on the next start.
    """

    def __init__(
        self,
        *,
        raw_ingestion_service: QQOfficialRawEventIngestionService,
        normalized_ingestion_service: QQOfficialNormalizedMessageIngestionService,
        interaction_dispatcher: QQOfficialInteractionDispatcher,
        outbox_repository: InteractionOutboxRepository,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._raw_ingestion_service = raw_ingestion_service
        self._normalized_ingestion_service = normalized_ingestion_service
        self._interaction_dispatcher = interaction_dispatcher
        self._outbox_repository = outbox_repository
        self._max_attempts = max_attempts

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
        if interaction_name is None:
            return QQOfficialEventProcessResult(
                transport=event.transport,
                event_type=event.event_type,
                raw_persisted=True,
                normalized_persisted=True,
                raw_event_id=raw_event.id,
                interaction_name=None,
            )

        group_id = normalized.context.group_id or ""
        trigger_message_id = normalized.platform_message_id or ""
        if not group_id or not trigger_message_id:
            logger.warning(
                "qq official interaction not enqueued: missing group_id or message_id raw_event_id=%s",
                raw_event.id,
            )
            return QQOfficialEventProcessResult(
                transport=event.transport,
                event_type=event.event_type,
                raw_persisted=True,
                normalized_persisted=True,
                raw_event_id=raw_event.id,
                interaction_name=interaction_name,
            )

        outbox_row, created = await self._outbox_repository.create_pending(
            platform=QQ_OFFICIAL_PLATFORM,
            group_id=group_id,
            trigger_message_id=trigger_message_id,
            interaction_name=interaction_name,
            source_raw_event_id=raw_event.id,
            max_attempts=self._max_attempts,
        )
        if not created:
            # Duplicate trigger: the work is already enqueued or
            # completed.  We do not schedule a second in-process task.
            return QQOfficialEventProcessResult(
                transport=event.transport,
                event_type=event.event_type,
                raw_persisted=True,
                normalized_persisted=True,
                raw_event_id=raw_event.id,
                interaction_name=interaction_name,
                outbox_id=outbox_row.id,
                outbox_already_existed=True,
            )

        return QQOfficialEventProcessResult(
            transport=event.transport,
            event_type=event.event_type,
            raw_persisted=True,
            normalized_persisted=True,
            raw_event_id=raw_event.id,
            interaction_name=interaction_name,
            outbox_id=outbox_row.id,
        )


__all__ = ["QQOfficialEventProcessor", "QQOfficialEventProcessResult"]
