"""End-to-end concurrency / lifecycle tests for QQ Official event processing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.adapters.qq_official.inbound import QQOfficialInboundEvent
from app.services.qq_official_event_processor import QQOfficialEventProcessor
from app.services.interaction_dispatch import QQOfficialInteractionDispatcher
from app.services.normalized_message_ingestion import (
    QQOfficialNormalizedMessageIngestionService,
)
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.storage.models import RawEvent


class _FakeRaw:
    def __init__(self, result: RawEvent) -> None:
        self._result = result

    async def ingest(self, event):  # type: ignore[no-untyped-def]
        return self._result


class _FakeNormalized:
    def __init__(self, result):  # type: ignore[no-untyped-def]
        self._result = result

    async def ingest(self, raw):  # type: ignore[no-untyped-def]
        return self._result


def _event(text: str = "hi") -> QQOfficialInboundEvent:
    payload = {
        "id": "x",
        "op": 0,
        "s": 1,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": "m",
            "timestamp": "2026-08-11T12:00:00+08:00",
            "group_openid": "g",
            "message_type": 0,
            "author": {"id": "u", "member_openid": "u"},
            "content": text,
        },
    }
    return QQOfficialInboundEvent(
        event_id="x",
        op=0,
        sequence=1,
        event_type="GROUP_MESSAGE_CREATE",
        data=payload["d"],
        transport="webhook",
        raw_payload=payload,
    )


def _raw() -> RawEvent:
    return RawEvent(
        id=1,
        platform="qq_official",
        received_at=datetime.now(timezone.utc),
        raw_payload={"d": {}},
        payload_hash="h",
    )


@pytest.mark.asyncio
async def test_processor_serializes_repeated_process_calls() -> None:
    raw_svc = _FakeRaw(_raw())
    norm_svc = _FakeNormalized(object())

    call_count = 0
    started = asyncio.Event()
    release = asyncio.Event()

    class _Disp:
        def __init__(self) -> None:
            self.handles: list[object] = []

        def interaction_name(self, msg):  # type: ignore[no-untyped-def]
            return "ping"

        async def handle(self, msg):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            self.handles.append(msg)
            if call_count == 1:
                started.set()
                await release.wait()
            return None

    disp = _Disp()
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=raw_svc,
        normalized_ingestion_service=norm_svc,
        interaction_dispatcher=disp,  # type: ignore[arg-type]
        max_concurrent_interactions=1,
    )

    result_one = await processor.process(_event())
    assert result_one.interaction_scheduled is True
    await asyncio.wait_for(started.wait(), timeout=1.0)

    # The slot is taken; further scheduling must be refused.
    for _ in range(3):
        r = await processor.process(_event())
        assert r.interaction_scheduled is False
    release.set()
    await processor.drain()
    await processor.aclose()


@pytest.mark.asyncio
async def test_processor_drain_times_out_when_workers_block() -> None:
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=_FakeRaw(_raw()),
        normalized_ingestion_service=_FakeNormalized(object()),
        interaction_dispatcher=type("D", (), {
            "interaction_name": lambda self, m: "p",
            "handle": lambda self, m: asyncio.Event().wait(),
        })(),
        max_concurrent_interactions=4,
    )
    await processor.process(_event())
    # Drain with a tiny timeout so we don't actually wait forever.
    await processor.drain(timeout=0.05)
    await processor.aclose()
