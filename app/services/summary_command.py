"""OneBot-only orchestration for the exact manual ``#总结`` command."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from enum import Enum
from typing import Protocol

from app.domain.messages import InternalMessage, ProvenanceSource, TextSegment
from app.domain.summaries import StoredSummary, SummaryResult
from app.rendering import SummaryMessageFormatter
from app.services.summary import SummaryService
from app.storage.summary_repository import SummaryRepository


logger = logging.getLogger(__name__)


class SummaryCommandStatus(str, Enum):
    DISABLED = "disabled"
    NOT_COMMAND = "not_command"
    INVALID_CONTEXT = "invalid_context"
    COOLDOWN = "cooldown"
    IN_PROGRESS = "in_progress"
    GENERATION_FAILED = "generation_failed"
    PERSISTENCE_FAILED = "persistence_failed"
    SEND_FAILED = "send_failed"
    SUCCEEDED = "succeeded"


class _SummaryGenerator(Protocol):
    async def summarize(self, **kwargs: object) -> SummaryResult: ...


class _SummaryStore(Protocol):
    async def persist(self, result: SummaryResult) -> StoredSummary: ...


class _GroupMessageSender(Protocol):
    async def send_group_message(self, group_id: str, message: str) -> object: ...


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
        self._state_lock = asyncio.Lock()
        self._active_groups: set[tuple[str, str]] = set()
        self._last_started: dict[tuple[str, str], float] = {}

    async def handle(
        self,
        message: InternalMessage,
        *,
        post_type: str | None,
        self_id: str | None,
    ) -> SummaryCommandStatus:
        """Handle a normalized inbound event without exposing failures upstream."""

        if not self._enabled:
            return SummaryCommandStatus.DISABLED
        if not is_summary_command(message):
            return SummaryCommandStatus.NOT_COMMAND
        if not self._valid_context(message, post_type=post_type, self_id=self_id):
            return SummaryCommandStatus.INVALID_CONTEXT

        group_id = message.context.group_id
        timestamp = message.timestamp
        assert group_id is not None
        assert timestamp is not None
        key = (message.platform, group_id)
        claim = await self._claim(key)
        if claim is not None:
            return claim

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
                return SummaryCommandStatus.GENERATION_FAILED

            try:
                await self._summary_repository.persist(result)
                logger.info("summary persisted")
            except Exception as error:
                logger.warning(
                    "summary persistence failed error_type=%s",
                    type(error).__name__,
                )
                return SummaryCommandStatus.PERSISTENCE_FAILED

            outbound = self._formatter.format(result)
            try:
                await self._sender.send_group_message(group_id, outbound)
                logger.info("summary send succeeded")
                return SummaryCommandStatus.SUCCEEDED
            except Exception as error:
                logger.warning(
                    "summary send failed error_type=%s",
                    type(error).__name__,
                )
                return SummaryCommandStatus.SEND_FAILED
        finally:
            async with self._state_lock:
                self._active_groups.discard(key)

    @staticmethod
    def _valid_context(
        message: InternalMessage,
        *,
        post_type: str | None,
        self_id: str | None,
    ) -> bool:
        if post_type != "message":
            return False
        if message.platform != "onebot11":
            return False
        if message.context.message_type != "group":
            return False
        if not message.context.group_id:
            return False
        timestamp = message.timestamp
        if timestamp is None or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return False
        actor_id = message.actor.user_id
        if self_id is not None and actor_id is not None and self_id == actor_id:
            return False
        return True

    async def _claim(
        self,
        key: tuple[str, str],
    ) -> SummaryCommandStatus | None:
        async with self._state_lock:
            if key in self._active_groups:
                return SummaryCommandStatus.IN_PROGRESS
            now = self._clock()
            last_started = self._last_started.get(key)
            if (
                last_started is not None
                and now - last_started < self._cooldown_seconds
            ):
                return SummaryCommandStatus.COOLDOWN
            self._active_groups.add(key)
            self._last_started[key] = now
            return None
