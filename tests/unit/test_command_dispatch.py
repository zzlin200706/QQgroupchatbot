from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.messages import (
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
    InternalMessage,
    MessageContext,
    MessageProvenance,
    ProvenanceSource,
    TextSegment,
)
from app.services.command_dispatch import QQOfficialCommandDispatcher
from app.services.ping_command import PingCommandStatus
from app.services.summary_command import SummaryCommandStatus


COMMAND_TIME = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def identity() -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id="actor-1",
        display_name="测试用户",
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )


def message(text: str) -> InternalMessage:
    actor = identity()
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id="message-1",
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="synthetic-group",
        ),
        actor=actor,
        author=actor,
        timestamp=COMMAND_TIME,
        segments=(TextSegment(position=0, raw_data={}, text=text),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=1,
        ),
    )


class FakePingHandler:
    def __init__(self) -> None:
        self.calls: list[InternalMessage] = []

    async def handle(self, value: InternalMessage) -> PingCommandStatus:
        self.calls.append(value)
        return PingCommandStatus.SUCCEEDED


class FakeSummaryHandler:
    def __init__(self) -> None:
        self.calls: list[InternalMessage] = []

    async def handle(self, value: InternalMessage) -> SummaryCommandStatus:
        self.calls.append(value)
        return SummaryCommandStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_dispatches_ping_without_calling_summary_handler() -> None:
    ping_handler = FakePingHandler()
    summary_handler = FakeSummaryHandler()
    dispatcher = QQOfficialCommandDispatcher(
        ping_handler=ping_handler, summary_handler=summary_handler
    )

    result = await dispatcher.handle(message("#ping"))

    assert result is PingCommandStatus.SUCCEEDED
    assert len(ping_handler.calls) == 1
    assert summary_handler.calls == []


@pytest.mark.asyncio
async def test_dispatches_summary_command_to_summary_handler() -> None:
    ping_handler = FakePingHandler()
    summary_handler = FakeSummaryHandler()
    dispatcher = QQOfficialCommandDispatcher(
        ping_handler=ping_handler, summary_handler=summary_handler
    )

    result = await dispatcher.handle(message("#总结"))

    assert result is SummaryCommandStatus.SUCCEEDED
    assert ping_handler.calls == []
    assert len(summary_handler.calls) == 1


@pytest.mark.asyncio
async def test_ordinary_text_is_ignored_without_calling_handlers() -> None:
    ping_handler = FakePingHandler()
    summary_handler = FakeSummaryHandler()
    dispatcher = QQOfficialCommandDispatcher(
        ping_handler=ping_handler, summary_handler=summary_handler
    )

    result = await dispatcher.handle(message("你好"))

    assert result is None
    assert ping_handler.calls == []
    assert summary_handler.calls == []
