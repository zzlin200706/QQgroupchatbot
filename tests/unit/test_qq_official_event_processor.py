"""Tests for the producer side of the durable interaction outbox.

The producer (``QQOfficialEventProcessor``) is responsible for
parsing, persisting the raw event, normalising, and writing a
``pending`` outbox row.  It does *not* run the LLM-bound work; that
is the consumer's job (``InteractionOutboxWorker``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
import pytest_asyncio

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
from app.storage.database import Database
from app.storage.interaction_outbox_repository import InteractionOutboxRepository
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


@dataclass
class _Environment:
    database: Database
    outbox_repo: InteractionOutboxRepository
    raw_event_repo: RawEventRepository


def identity() -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id="user-1",
        display_name="测试用户",
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )


def inbound_event(text: str = "hello", *, message_id: str = "message-1") -> QQOfficialInboundEvent:
    payload = {
        "id": "event-1",
        "op": 0,
        "s": 1,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": message_id,
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


async def _persist_raw(env: _Environment, payload_hash: str = "hash") -> RawEvent:
    raw = RawEvent(
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
        payload_hash=payload_hash,
    )
    return await env.raw_event_repo.insert(raw)


def internal_message(text: str = "hello", *, message_id: str = "message-1") -> InternalMessage:
    actor = identity()
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id=message_id,
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


class PersistingRawIngestion:
    """A raw ingestion service that writes to the real repo."""

    def __init__(self, env: _Environment) -> None:
        self.env = env
        self.calls: list[QQOfficialInboundEvent] = []

    async def ingest(self, event: QQOfficialInboundEvent) -> RawEvent | None:
        self.calls.append(event)
        return await _persist_raw(self.env)


class NoneRawIngestion:
    def __init__(self) -> None:
        self.calls: list[QQOfficialInboundEvent] = []

    async def ingest(self, event: QQOfficialInboundEvent) -> RawEvent | None:
        self.calls.append(event)
        return None


class ScriptedNormalizedIngestion:
    def __init__(self, result: InternalMessage | None) -> None:
        self.result = result
        self.calls: list[RawEvent] = []

    async def ingest(self, event: RawEvent) -> InternalMessage | None:
        self.calls.append(event)
        return self.result


class FakeDispatcher:
    def __init__(self, *, interaction_name: str | None) -> None:
        self._interaction_name = interaction_name
        self.interaction_name_calls: list[InternalMessage] = []
        self.handle_calls: list[InternalMessage] = []

    def interaction_name(self, message: InternalMessage) -> str | None:
        self.interaction_name_calls.append(message)
        return self._interaction_name

    async def handle(self, message: InternalMessage) -> object:
        self.handle_calls.append(message)
        return object()


@pytest_asyncio.fixture
async def env(tmp_path) -> _Environment:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'proc.db'}")
    await db.initialize()
    return _Environment(
        database=db,
        outbox_repo=InteractionOutboxRepository(db.session_factory),
        raw_event_repo=RawEventRepository(db.session_factory),
    )


@pytest.mark.asyncio
async def test_process_persists_raw_and_normalized_for_ordinary_message(
    env: _Environment,
) -> None:
    raw = PersistingRawIngestion(env)
    normalized = ScriptedNormalizedIngestion(internal_message("hello"))
    dispatcher = FakeDispatcher(interaction_name=None)
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
        outbox_repository=env.outbox_repo,
    )

    result = await processor.process(inbound_event("hello"))

    assert result.raw_persisted is True
    assert result.normalized_persisted is True
    assert result.interaction_name is None
    assert result.outbox_id is None
    assert len(raw.calls) == 1
    assert len(normalized.calls) == 1
    assert len(dispatcher.interaction_name_calls) == 1


@pytest.mark.asyncio
async def test_recognised_interaction_creates_pending_outbox_row(
    env: _Environment,
) -> None:
    raw = PersistingRawIngestion(env)
    normalized = ScriptedNormalizedIngestion(internal_message("#ping"))
    dispatcher = FakeDispatcher(interaction_name="ping")
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
        outbox_repository=env.outbox_repo,
    )

    result = await processor.process(inbound_event("#ping"))

    assert result.interaction_name == "ping"
    assert result.outbox_id is not None
    assert result.outbox_already_existed is False

    row = await env.outbox_repo.get_by_id(result.outbox_id)
    assert row is not None
    assert row.status == "pending"
    assert row.attempt_count == 0
    assert row.platform == "qq_official"
    assert row.group_id == "group-1"
    assert row.trigger_message_id == "message-1"
    assert row.interaction_name == "ping"
    assert row.source_raw_event_id == result.raw_event_id


@pytest.mark.asyncio
async def test_duplicate_trigger_returns_existing_outbox_row(
    env: _Environment,
) -> None:
    raw = PersistingRawIngestion(env)
    normalized = ScriptedNormalizedIngestion(internal_message("#ping"))
    dispatcher = FakeDispatcher(interaction_name="ping")
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
        outbox_repository=env.outbox_repo,
    )

    first = await processor.process(inbound_event("#ping"))
    second = await processor.process(inbound_event("#ping"))

    assert first.outbox_id == second.outbox_id
    assert first.outbox_already_existed is False
    assert second.outbox_already_existed is True


@pytest.mark.asyncio
async def test_raw_persistence_failure_returns_failure_result(
    env: _Environment,
) -> None:
    raw = NoneRawIngestion()
    normalized = ScriptedNormalizedIngestion(internal_message("hello"))
    dispatcher = FakeDispatcher(interaction_name=None)
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
        outbox_repository=env.outbox_repo,
    )

    result = await processor.process(inbound_event("hello"))

    assert result.raw_persisted is False
    assert result.normalized_persisted is False
    assert normalized.calls == []
    assert dispatcher.interaction_name_calls == []


@pytest.mark.asyncio
async def test_normalized_failure_keeps_raw_and_skips_outbox(
    env: _Environment,
) -> None:
    raw = PersistingRawIngestion(env)
    normalized = ScriptedNormalizedIngestion(None)
    dispatcher = FakeDispatcher(interaction_name="ping")
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
        outbox_repository=env.outbox_repo,
    )

    result = await processor.process(inbound_event("hello"))

    assert result.raw_persisted is True
    assert result.normalized_persisted is False
    assert result.outbox_id is None
    assert dispatcher.interaction_name_calls == []


@pytest.mark.asyncio
async def test_missing_group_id_does_not_create_outbox_row(
    env: _Environment,
) -> None:
    raw = PersistingRawIngestion(env)
    normalized_actor = identity()
    no_group = InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id="message-1",
        context=MessageContext(message_type="0", sub_type="GROUP_MESSAGE_CREATE", group_id=None),
        actor=normalized_actor,
        author=normalized_actor,
        timestamp=datetime(2026, 8, 11, 4, tzinfo=timezone.utc),
        segments=(TextSegment(position=0, raw_data="#ping", text="#ping"),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT, raw_event_id=1
        ),
    )
    normalized = ScriptedNormalizedIngestion(no_group)
    dispatcher = FakeDispatcher(interaction_name="ping")
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
        outbox_repository=env.outbox_repo,
    )

    result = await processor.process(inbound_event("#ping"))

    assert result.interaction_name == "ping"
    assert result.outbox_id is None
    stats = await env.outbox_repo.stats()
    assert stats["pending"] == 0


@pytest.mark.asyncio
async def test_interaction_scheduled_alias_is_true_for_new_rows(
    env: _Environment,
) -> None:
    raw = PersistingRawIngestion(env)
    normalized = ScriptedNormalizedIngestion(internal_message("#ping"))
    dispatcher = FakeDispatcher(interaction_name="ping")
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
        outbox_repository=env.outbox_repo,
    )

    result = await processor.process(inbound_event("#ping"))

    # The legacy 'interaction_scheduled' field is True when we created
    # a new outbox row and False for duplicates / refusals.
    assert result.interaction_scheduled is True


@pytest.mark.asyncio
async def test_interaction_scheduled_alias_is_false_for_duplicates(
    env: _Environment,
) -> None:
    raw = PersistingRawIngestion(env)
    normalized = ScriptedNormalizedIngestion(internal_message("#ping"))
    dispatcher = FakeDispatcher(interaction_name="ping")
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw,
        normalized_ingestion_service=normalized,
        interaction_dispatcher=dispatcher,
        outbox_repository=env.outbox_repo,
    )

    await processor.process(inbound_event("#ping"))
    second = await processor.process(inbound_event("#ping"))

    assert second.interaction_scheduled is False
