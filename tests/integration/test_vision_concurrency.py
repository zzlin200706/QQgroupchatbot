"""Tests for vision-pipeline concurrency and isolation.

The vision path is part of the LLM-bound work, so it is bounded
by the same outbox-worker semaphore as text interactions.  This
file documents the design choice and verifies it is consistent
across mixed text + image traffic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.domain.images import ImageDescription, ResolvedImage
from app.domain.messages import (
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
    ImageSegment,
    InternalMessage,
    MessageContext,
    MessageProvenance,
    ProvenanceSource,
    TextSegment,
)
from app.llm.providers.base import LLMProvider, VisionProvider
from app.services.image_enrichment import ImageEnrichmentService
from app.services.image_resources import ImageResourceResolver, ImageResolveError
from app.services.interaction_outbox_worker import InteractionOutboxWorker
from app.services.command_status import CommandStatus
from app.storage.database import Database
from app.storage.interaction_outbox_repository import InteractionOutboxRepository
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


def _valid_payload(message_id: str = "m-1", with_image: bool = False) -> dict:
    d = {
        "id": message_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "group_openid": "g-1",
        "message_type": 7 if with_image else 0,
        "author": {"id": "user-1", "member_openid": "user-1"},
        "content": "hi",
    }
    if with_image:
        d["attachments"] = [
            {
                "content_type": "image/jpeg",
                "url": "https://example.invalid/x.jpg",
            }
        ]
    return {
        "id": "event-1",
        "op": 0,
        "s": 1,
        "t": "GROUP_MESSAGE_CREATE",
        "d": d,
    }


def _identity() -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id="user-1",
        display_name="u",
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )


def _text_message(message_id: str = "m-1") -> InternalMessage:
    actor = _identity()
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id=message_id,
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="g-1",
        ),
        actor=actor,
        author=actor,
        timestamp=datetime(2026, 8, 11, 4, tzinfo=timezone.utc),
        segments=(TextSegment(position=0, raw_data="hi", text="hi"),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT, raw_event_id=1
        ),
    )


def _image_message(message_id: str = "m-1") -> InternalMessage:
    actor = _identity()
    seg = ImageSegment(
        position=0,
        raw_data=None,
        file=None,
        url="https://example.invalid/x.jpg",
        summary=None,
        sub_type=None,
        content_type="image/jpeg",
    )
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id=message_id,
        context=MessageContext(
            message_type="7",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="g-1",
        ),
        actor=actor,
        author=actor,
        timestamp=datetime(2026, 8, 11, 4, tzinfo=timezone.utc),
        segments=(seg,),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT, raw_event_id=1
        ),
    )


class FakeResolver:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def resolve(self, image: ImageSegment) -> ResolvedImage:
        self.calls.append(image.url or "")
        if self.fail:
            raise ImageResolveError("forced")
        return ResolvedImage(
            data=b"\xff\xd8\xff\xe0fake-jpeg",
            content_type="image/jpeg",
        )


class FakeVisionProvider:
    def __init__(self) -> None:
        self.descriptions: list[ImageDescription] = []
        self.calls = 0

    @property
    def supports_vision(self) -> bool:
        return True

    async def describe_image(
        self, image: ResolvedImage, *, prompt: str
    ) -> ImageDescription:
        self.calls += 1
        return ImageDescription(description=f"desc-{self.calls}")

    async def aclose(self) -> None:
        pass


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'vision.db'}")
    await db.initialize()
    yield type("Env", (), {
        "database": db,
        "outbox_repo": InteractionOutboxRepository(db.session_factory),
        "raw_event_repo": RawEventRepository(db.session_factory),
    })()
    await db.dispose()


async def _create_text_row(env, *, message_id: str) -> RawEvent:
    raw = RawEvent(
        platform="qq_official",
        received_at=datetime.now(timezone.utc),
        raw_payload=_valid_payload(message_id=message_id),
        payload_hash=f"h-{message_id}",
    )
    return await env.raw_event_repo.insert(raw)


@pytest.mark.asyncio
async def test_vision_and_text_share_one_outbox_semaphore(env) -> None:
    """Mixed text + image traffic is bounded by the single LLM semaphore.

    The LLM semaphore is the only bound on the worker's side.  A
    burst of N image interactions and N text interactions is
    processed with at most ``max_concurrent`` total in flight,
    never more.
    """

    # 4 text + 4 image rows = 8 total.
    for i in range(4):
        raw = await _create_text_row(env, message_id=f"t-{i}")
        await env.outbox_repo.create_pending(
            platform="qq_official",
            group_id="g-1",
            trigger_message_id=f"t-{i}",
            interaction_name="grounded_qa",
            source_raw_event_id=raw.id,
        )
    for i in range(4):
        raw = RawEvent(
            platform="qq_official",
            received_at=datetime.now(timezone.utc),
            raw_payload=_valid_payload(message_id=f"i-{i}", with_image=True),
            payload_hash=f"hi-{i}",
        )
        await env.raw_event_repo.insert(raw)
        await env.outbox_repo.create_pending(
            platform="qq_official",
            group_id="g-1",
            trigger_message_id=f"i-{i}",
            interaction_name="grounded_qa",
            source_raw_event_id=raw.id,
        )

    # Build a dispatch function that calls ImageEnrichmentService
    # for image messages, then pretends to send a reply.
    vision = FakeVisionProvider()
    resolver = FakeResolver()
    enrichment = ImageEnrichmentService(
        enabled=True, resolver=resolver, provider=vision
    )

    in_flight = 0
    peak = 0
    counter = asyncio.Lock()

    async def dispatch(message: InternalMessage) -> object:
        nonlocal in_flight, peak
        if any(isinstance(seg, ImageSegment) for seg in message.segments):
            await enrichment.enrich_messages((message,))
        async with counter:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        async with counter:
            in_flight -= 1
        return CommandStatus.SUCCEEDED

    worker = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=dispatch,
        max_concurrent=2,
        num_workers=1,
        poll_interval_seconds=0.02,
    )
    await worker.start()
    try:
        for _ in range(200):
            stats = await env.outbox_repo.stats()
            if stats["pending"] == 0 and stats["running"] == 0:
                break
            await asyncio.sleep(0.05)
    finally:
        await worker.stop()

    assert peak <= 2, f"peak in-flight = {peak}, expected <= 2"
    stats = await env.outbox_repo.stats()
    assert stats["completed"] == 8
    # The 4 image rows should have triggered 4 vision calls.
    assert vision.calls == 4
    assert len(resolver.calls) == 4


@pytest.mark.asyncio
async def test_image_download_failure_does_not_crash_interaction(env) -> None:
    """A failing image resolver must not block the rest of the work."""

    # One image row, then one text row.
    raw_img = RawEvent(
        platform="qq_official",
        received_at=datetime.now(timezone.utc),
        raw_payload=_valid_payload(message_id="i-1", with_image=True),
        payload_hash="hi-1",
    )
    await env.raw_event_repo.insert(raw_img)
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="i-1",
        interaction_name="grounded_qa",
        source_raw_event_id=raw_img.id,
    )
    raw_text = await _create_text_row(env, message_id="t-1")
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="t-1",
        interaction_name="grounded_qa",
        source_raw_event_id=raw_text.id,
    )

    vision = FakeVisionProvider()
    resolver = FakeResolver(fail=True)  # always fail image downloads
    enrichment = ImageEnrichmentService(
        enabled=True, resolver=resolver, provider=vision
    )

    async def dispatch(message: InternalMessage) -> object:
        if any(isinstance(seg, ImageSegment) for seg in message.segments):
            await enrichment.enrich_messages((message,))
        return CommandStatus.SUCCEEDED

    worker = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=dispatch,
        max_concurrent=2,
        num_workers=1,
        poll_interval_seconds=0.02,
    )
    await worker.start()
    try:
        for _ in range(200):
            stats = await env.outbox_repo.stats()
            if stats["pending"] == 0 and stats["running"] == 0:
                break
            await asyncio.sleep(0.05)
    finally:
        await worker.stop()

    # Both rows completed (the failing image enrichment just records
    # an "unavailable" description, then the row completes normally).
    stats = await env.outbox_repo.stats()
    assert stats["completed"] == 2
    assert stats["failed"] == 0
    # Vision was never actually called because the resolver failed first.
    assert vision.calls == 0
