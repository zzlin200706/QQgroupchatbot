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
from app.services.ping_command import (
    PingCommandHandler,
    PingCommandStatus,
    is_ping_command,
)


COMMAND_TIME = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def identity(user_id: str | None = "actor-1") -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id=user_id,
        display_name="测试用户" if user_id is not None else None,
        card=None,
        source=IdentitySource.EVENT if user_id is not None else IdentitySource.UNKNOWN,
        availability=(
            IdentityAvailability.KNOWN
            if user_id is not None
            else IdentityAvailability.UNKNOWN
        ),
    )


def message(
    text: str = "#ping",
    *,
    platform: str = "qq_official",
    sub_type: str | None = "GROUP_MESSAGE_CREATE",
    group_id: str | None = "synthetic-group",
    platform_message_id: str | None = "message-1",
    provenance: ProvenanceSource = ProvenanceSource.DIRECT_EVENT,
) -> InternalMessage:
    actor = identity()
    return InternalMessage(
        platform=platform,
        source_raw_event_id=1,
        platform_message_id=platform_message_id,
        context=MessageContext(
            message_type="0",
            sub_type=sub_type,
            group_id=group_id,
        ),
        actor=actor,
        author=actor,
        timestamp=COMMAND_TIME,
        segments=(TextSegment(position=0, raw_data={}, text=text),),
        provenance=MessageProvenance(
            source_type=provenance,
            raw_event_id=1,
        ),
    )


class FakeSender:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, str | None]] = []

    async def send_group_message(
        self,
        group_id: str,
        message: str,
        *,
        msg_id: str | None = None,
    ) -> object:
        self.calls.append((group_id, message, msg_id))
        if self.error is not None:
            raise self.error
        return object()


@pytest.mark.parametrize("text", ["#ping", " #ping ", "\n#ping\t"])
def test_detects_only_exact_trimmed_top_level_ping_text(text: str) -> None:
    assert is_ping_command(message(text))


@pytest.mark.parametrize("text", ["ping", "#ping now", "abc #ping", "#总结"])
def test_rejects_non_exact_ping_text(text: str) -> None:
    assert not is_ping_command(message(text))


@pytest.mark.asyncio
async def test_ping_command_sends_pong_to_trigger_group_and_message() -> None:
    sender = FakeSender()
    handler = PingCommandHandler(sender=sender)

    status = await handler.handle(message())

    assert status is PingCommandStatus.SUCCEEDED
    assert sender.calls == [("synthetic-group", "pong", "message-1")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        message(group_id=None),
        message(platform="other_platform"),
        message(sub_type="GROUP_AT_MESSAGE_CREATE"),
        message(platform_message_id=None),
    ],
)
async def test_invalid_context_never_sends(candidate: InternalMessage) -> None:
    sender = FakeSender()
    handler = PingCommandHandler(sender=sender)

    status = await handler.handle(candidate)

    assert status is PingCommandStatus.INVALID_CONTEXT
    assert sender.calls == []


@pytest.mark.asyncio
async def test_non_command_is_ignored() -> None:
    sender = FakeSender()
    handler = PingCommandHandler(sender=sender)

    status = await handler.handle(message("你好"))

    assert status is PingCommandStatus.NOT_COMMAND
    assert sender.calls == []


@pytest.mark.asyncio
async def test_send_failure_is_safe() -> None:
    sender = FakeSender(error=RuntimeError("synthetic failure"))
    handler = PingCommandHandler(sender=sender)

    status = await handler.handle(message())

    assert status is PingCommandStatus.SEND_FAILED
    assert sender.calls == [("synthetic-group", "pong", "message-1")]
