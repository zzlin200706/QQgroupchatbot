from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.adapters.qq_official.inbound import QQOfficialInboundEvent
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
from app.services.qq_official_event_processor import QQOfficialEventProcessor
from app.storage.models import RawEvent


def identity() -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id="user-1",
        display_name="测试用户",
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )


def inbound_event(text: str = "hello") -> QQOfficialInboundEvent:
    payload = {
        "id": "event-1",
        "op": 0,
        "s": 1,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": "message-1",
            "timestamp": "2026-08-11T12:00:00+08:00",
            "group_openid": "group-1",
            "message_type": 0,
            "author": {"id": "user-1", "member_openid": "user-1"},
            "content": text,
        },
    }
    return QQOfficialInboundEvent(
        event_id="event-1",
        op=0,
        sequence=1,
        event_type="GROUP_MESSAGE_CREATE",
        data=payload["d"],
        transport="webhook",
        raw_payload=payload,
    )


def raw_event() -> RawEvent:
    return RawEvent(
        id=1,
        platform="qq_official",
        received_at=datetime.now(timezone.utc),
        event_time=datetime.now(timezone.utc),
        post_type=None,
        message_type="0",
        sub_type="GROUP_MESSAGE_CREATE",
        self_id=None,
        user_id="user-1",
        group_id="group-1",
        message_id="message-1",
        raw_payload={},
        payload_hash="hash",
    )


def internal_message(text: str = "hello") -> InternalMessage:
    actor = identity()
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id="message-1",
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="group-1",
        ),
        actor=actor,
        author=actor,
        timestamp=datetime(2026, 8, 11, 4, tzinfo=timezone.utc),
        segments=(TextSegment(position=0, raw_data=text, text=text),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=1,
        ),
    )


class FakeRawIngestion:
    def __init__(self, *, result: RawEvent | None) -> None:
        self.result = result
        self.calls: list[QQOfficialInboundEvent] = []

    async def ingest(self, event: QQOfficialInboundEvent) -> RawEvent | None:
        self.calls.append(event)
        return self.result


class FakeNormalizedIngestion:
    def __init__(self, *, result: InternalMessage | None) -> None:
        self.result = result
        self.calls: list[RawEvent] = []

    async def ingest(self, event: RawEvent) -> InternalMessage | None:
        self.calls.append(event)
        return self.result


class FakeDispatcher:
    def __init__(
        self,
        *,
        interaction_name: str | None,
        error: Exception | None = None,
    ) -> None:
        self._interaction_name = interaction_name
        self._error = error
        self.interaction_name_calls: list[InternalMessage] = []
        self.handle_calls: list[InternalMessage] = []

    def interaction_name(self, message: InternalMessage) -> str | None:
        self.interaction_name_calls.append(message)
        return self._interaction_name

    async def handle(self, message: InternalMessage) -> object:
        self.handle_calls.append(message)
        if self._error is not None:
            raise self._error
        await asyncio.sleep(0)
        return object()


@pytest.mark.asyncio
async def test_process_persists_raw_before_normalized_and_ignores_ordinary_message() -> None:
    raw = FakeRawIngestion(result=raw_event())
    normalized = FakeNormalizedIngestion(result=internal_message("hello"))
    dispatcher = FakeDispatcher(interaction_name=None)
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
    )

    result = await processor.process(inbound_event("hello"))

    assert result.raw_persisted is True
    assert result.normalized_persisted is True
    assert result.command_name is None
    assert len(raw.calls) == 1
    assert len(normalized.calls) == 1
    assert len(dispatcher.interaction_name_calls) == 1
    assert dispatcher.handle_calls == []


@pytest.mark.asyncio
async def test_ping_schedules_ping_handler_only() -> None:
    raw = FakeRawIngestion(result=raw_event())
    normalized = FakeNormalizedIngestion(result=internal_message("#ping"))
    dispatcher = FakeDispatcher(interaction_name="ping")
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
    )

    result = await processor.process(inbound_event("#ping"))
    await processor.drain()

    assert result.command_name == "ping"
    assert len(dispatcher.handle_calls) == 1
    assert dispatcher.handle_calls[0].segments[0].text == "#ping"


@pytest.mark.asyncio
async def test_summary_schedules_summary_handler_only() -> None:
    raw = FakeRawIngestion(result=raw_event())
    normalized = FakeNormalizedIngestion(result=internal_message("#总结"))
    dispatcher = FakeDispatcher(interaction_name="summary")
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
    )

    result = await processor.process(inbound_event("#总结"))
    await processor.drain()

    assert result.command_name == "summary"
    assert len(dispatcher.handle_calls) == 1
    assert dispatcher.handle_calls[0].segments[0].text == "#总结"


@pytest.mark.asyncio
async def test_raw_persistence_failure_does_not_masquerade_as_success() -> None:
    raw = FakeRawIngestion(result=None)
    normalized = FakeNormalizedIngestion(result=internal_message("hello"))
    dispatcher = FakeDispatcher(interaction_name=None)
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
    )

    result = await processor.process(inbound_event("hello"))

    assert result.raw_persisted is False
    assert result.normalized_persisted is False
    assert normalized.calls == []
    assert dispatcher.interaction_name_calls == []


@pytest.mark.asyncio
async def test_parser_failure_keeps_raw_and_skips_command() -> None:
    raw = FakeRawIngestion(result=raw_event())
    normalized = FakeNormalizedIngestion(result=None)
    dispatcher = FakeDispatcher(interaction_name="ping")
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
    )

    result = await processor.process(inbound_event("hello"))

    assert result.raw_persisted is True
    assert result.normalized_persisted is False
    assert dispatcher.interaction_name_calls == []
    assert dispatcher.handle_calls == []


@pytest.mark.asyncio
async def test_command_task_failure_is_logged_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = FakeRawIngestion(result=raw_event())
    normalized = FakeNormalizedIngestion(result=internal_message("#ping"))
    dispatcher = FakeDispatcher(interaction_name="ping", error=RuntimeError("boom"))
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
    )
    caplog.set_level("ERROR", logger="app.services.qq_official_event_processor")

    await processor.process(inbound_event("#ping"))
    await processor.drain()

    assert "qq official interaction task failed" in caplog.text
    assert "boom" not in caplog.text
