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
from app.services.interaction_dispatch import QQOfficialInteractionDispatcher


def message(text: str) -> InternalMessage:
    actor = IdentityRef(
        platform="qq_official",
        user_id="user-a",
        display_name=None,
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id="message-1",
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="group-a",
        ),
        actor=actor,
        author=actor,
        timestamp=datetime(2026, 8, 11, tzinfo=timezone.utc),
        segments=(TextSegment(position=0, raw_data=text, text=text),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=1,
        ),
    )


class FakeCommands:
    def __init__(self, name: str | None) -> None:
        self.name = name
        self.handled = 0

    def command_name(self, _: InternalMessage) -> str | None:
        return self.name

    async def handle(self, _: InternalMessage) -> str:
        self.handled += 1
        return "command"


class FakeAssistant:
    def __init__(self, detected: bool) -> None:
        self.detected = detected
        self.handled = 0

    def detect(self, _: InternalMessage) -> object | None:
        if not self.detected:
            return None
        return type("Trigger", (), {"trigger_type": type("Type", (), {"value": "grounded_qa"})()})()

    async def handle(self, _: InternalMessage) -> str:
        self.handled += 1
        return "assistant"


@pytest.mark.asyncio
async def test_explicit_command_has_precedence_over_assistant() -> None:
    commands = FakeCommands("summary")
    assistant = FakeAssistant(True)
    dispatcher = QQOfficialInteractionDispatcher(
        command_dispatcher=commands,  # type: ignore[arg-type]
        assistant_handler=assistant,  # type: ignore[arg-type]
    )

    assert dispatcher.interaction_name(message("#总结")) == "summary"
    assert await dispatcher.handle(message("#总结")) == "command"
    assert commands.handled == 1
    assert assistant.handled == 0


@pytest.mark.asyncio
async def test_assistant_runs_only_when_no_command_matches() -> None:
    commands = FakeCommands(None)
    assistant = FakeAssistant(True)
    dispatcher = QQOfficialInteractionDispatcher(
        command_dispatcher=commands,  # type: ignore[arg-type]
        assistant_handler=assistant,  # type: ignore[arg-type]
    )

    assert dispatcher.interaction_name(message("#问 问题")) == "grounded_qa"
    assert await dispatcher.handle(message("#问 问题")) == "assistant"
    assert assistant.handled == 1


@pytest.mark.asyncio
async def test_ordinary_message_has_no_interaction() -> None:
    commands = FakeCommands(None)
    assistant = FakeAssistant(False)
    dispatcher = QQOfficialInteractionDispatcher(
        command_dispatcher=commands,  # type: ignore[arg-type]
        assistant_handler=assistant,  # type: ignore[arg-type]
    )

    assert dispatcher.interaction_name(message("普通消息")) is None
    assert await dispatcher.handle(message("普通消息")) is None
    assert assistant.handled == 0
