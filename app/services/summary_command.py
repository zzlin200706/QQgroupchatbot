"""QQ Official orchestration for the exact manual ``#总结`` command."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol

from app.services.command_status import CommandStatus, is_valid_command_context

from app.domain.messages import InternalMessage, ProvenanceSource, TextSegment
from app.domain.summaries import StoredSummary, SummaryResult
from app.rendering import SummaryMessageFormatter
from app.services.summary import SummaryService
from app.storage.summary_repository import SummaryRepository


logger = logging.getLogger(__name__)


class _SummaryGenerator(Protocol):
    async def summarize(self, **kwargs: object) -> SummaryResult: ...


class _SummaryStore(Protocol):
    async def persist(self, result: SummaryResult) -> StoredSummary: ...


class _GroupMessageSender(Protocol):
    async def send_group_message(
        self,
        group_id: str,
        message: str,
        *,
        msg_id: str | None = None,
    ) -> object: ...


class _CommandClaimStore(Protocol):
    async def claim(self, **kwargs: str) -> bool: ...


def is_summary_command(message: InternalMessage) -> bool:
    """Match only exact text from the direct message's top-level segments."""

    if message.provenance.source_type is not ProvenanceSource.DIRECT_EVENT:
        return False
    if not message.segments or any(
        not isinstance(segment, TextSegment) for segment in message.segments
    ):
        return False
    return "".join(segment.text or "" for segment in message.segments).strip() == "#总结"


class SummaryCommandHandler:
    """Generate, persist, format, and send one bounded manual summary."""

    def __init__(
        self,
        *,
        summary_service: SummaryService | _SummaryGenerator,
        summary_repository: SummaryRepository | _SummaryStore,
        formatter: SummaryMessageFormatter,
        sender: _GroupMessageSender,
        enabled: bool = False,
        lookback_minutes: int = 120,
        cooldown_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
        claim_repository: _CommandClaimStore | None = None,
    ) -> None:
        if lookback_minutes < 1:
            raise ValueError("lookback_minutes must be at least one")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        self._summary_service = summary_service
        self._summary_repository = summary_repository
        self._formatter = formatter
        self._sender = sender
        self._enabled = enabled
        self._lookback_minutes = lookback_minutes
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._claim_repository = claim_repository
        self._active_groups: set[tuple[str, str]] = set()
        self._last_started: dict[tuple[str, str], float] = {}

    async def handle(
        self,
        message: InternalMessage,
    ) -> CommandStatus:
        """Handle a normalized inbound event without exposing failures upstream."""

        if not self._enabled:
            return CommandStatus.DISABLED
        if not is_summary_command(message):
            return CommandStatus.NOT_COMMAND
        if not is_valid_command_context(message):
            return CommandStatus.INVALID_CONTEXT

        group_id = message.context.group_id
        timestamp = message.timestamp
        assert group_id is not None
        assert timestamp is not None
        assert message.platform_message_id is not None
        if self._claim_repository is not None:
            try:
                claimed = await self._claim_repository.claim(
                    platform=message.platform,
                    group_id=group_id,
                    trigger_message_id=message.platform_message_id,
                    command_name="summary",
                )
            except Exception as error:
                logger.warning(
                    "summary command claim failed error_type=%s",
                    type(error).__name__,
                )
                return CommandStatus.PERSISTENCE_FAILED
            if not claimed:
                logger.info("duplicate summary command ignored")
                return CommandStatus.DUPLICATE
        key = (message.platform, group_id)
        if key in self._active_groups:
            return CommandStatus.IN_PROGRESS
        now = self._clock()
        last_started = self._last_started.get(key)
        if (
            last_started is not None
            and now - last_started < self._cooldown_seconds
        ):
            return CommandStatus.COOLDOWN
        self._active_groups.add(key)
        self._last_started[key] = now

        logger.info("summary command detected")
        try:
            try:
                result = await self._summary_service.summarize(
                    platform=message.platform,
                    group_id=group_id,
                    start_time=timestamp
                    - timedelta(minutes=self._lookback_minutes),
                    end_time=timestamp,
                )
                logger.info("summary generated")
            except Exception as error:
                logger.warning(
                    "summary generation failed error_type=%s",
                    type(error).__name__,
                )
                return CommandStatus.GENERATION_FAILED

            try:
                await self._summary_repository.persist(result)
                logger.info("summary persisted")
            except Exception as error:
                logger.warning(
                    "summary persistence failed error_type=%s",
                    type(error).__name__,
                )
                return CommandStatus.PERSISTENCE_FAILED

            outbound = self._formatter.format(result)
            try:
                await self._sender.send_group_message(
                    group_id,
                    outbound,
                    msg_id=message.platform_message_id,
                )
                logger.info("summary send succeeded")
                return CommandStatus.SUCCEEDED
            except Exception as error:
                logger.warning(
                    "summary send failed error_type=%s",
                    type(error).__name__,
                )
                return CommandStatus.SEND_FAILED
        finally:
            self._active_groups.discard(key)

