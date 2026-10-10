"""End-to-end concurrency / lifecycle tests for the outbox worker.

The processor is now producer-only (writes a pending row).  The
bounded LLM concurrency lives in the :class:`InteractionOutboxWorker`.
These tests exercise the worker's behaviour: slot acquisition,
drain timeout, refusal on saturation, and restart safety.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio

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
from app.services.interaction_outbox_worker import InteractionOutboxWorker
from app.storage.database import Database
from app.storage.interaction_outbox_repository import InteractionOutboxRepository
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


def _identity() -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id="user-1",
        display_name="u",
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )


def _message(*, message_id: str = "m") -> InternalMessage:
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


def _valid_payload(message_id: str = "message-1") -> dict:
    return {
        "id": "event-1",
        "op": 0,
        "s": 1,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": message_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "group_openid": "g-1",
            "message_type": 0,
            "author": {"id": "user-1", "member_openid": "user-1"},
            "content": "hi",
        },
    }


async def _persist_raw(env, payload_hash: str = "h", *, message_id: str = "message-1") -> RawEvent:
    raw = RawEvent(
        platform="qq_official",
        received_at=datetime.now(timezone.utc),
        raw_payload=_valid_payload(message_id=message_id),
        payload_hash=payload_hash,
    )
    return await env.raw_event_repo.insert(raw)


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    await db.initialize()
    outbox_repo = InteractionOutboxRepository(db.session_factory)
    raw_event_repo = RawEventRepository(db.session_factory)
    yield type("Env", (), {
        "database": db,
        "outbox_repo": outbox_repo,
        "raw_event_repo": raw_event_repo,
    })()
    await db.dispose()


@pytest.mark.asyncio
async def test_worker_saturates_after_configured_bound(env) -> None:
    """With max_concurrent=1, a second row stays pending while the first runs."""

    first_raw = await _persist_raw(env, payload_hash="a", message_id="m-1")
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=first_raw.id,
    )
    second_raw = await _persist_raw(env, payload_hash="b", message_id="m-2")
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-2",
        interaction_name="ping",
        source_raw_event_id=second_raw.id,
    )

    started = asyncio.Event()
    release = asyncio.Event()
    handle_started = 0

    async def handle(message: InternalMessage):
        nonlocal handle_started
        handle_started += 1
        if handle_started == 1:
            started.set()
            await release.wait()
        return None

    worker = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=handle,
        max_concurrent=1,
        num_workers=1,
        poll_interval_seconds=0.05,
    )
    await worker.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        # Give the worker a moment to attempt the second claim.
        await asyncio.sleep(0.2)
        stats = await env.outbox_repo.stats()
        assert stats["running"] == 1, stats
        assert stats["pending"] == 1, stats
    finally:
        release.set()
        # The second row will be picked up and processed normally
        # after the first releases.  ``stop`` waits for that to
        # complete (it cancels the worker task if it is still
        # running).
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_stop_drains_within_bounded_timeout(env) -> None:
    """``stop`` returns promptly even if a worker is mid-flight."""

    first_raw = await _persist_raw(env, payload_hash="a", message_id="m-1")
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=first_raw.id,
    )

    block = asyncio.Event()

    async def handle(message: InternalMessage):
        await block.wait()
        return None

    worker = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=handle,
        max_concurrent=1,
        num_workers=1,
        poll_interval_seconds=0.05,
    )
    await worker.start()
    await asyncio.sleep(0.2)

    started_at = asyncio.get_event_loop().time()
    # Don't unblock; the stop() should still complete promptly because
    # we cancel the worker tasks.
    try:
        await asyncio.wait_for(worker.stop(), timeout=2.0)
    finally:
        block.set()
    elapsed = asyncio.get_event_loop().time() - started_at
    assert elapsed < 1.0, f"stop() took {elapsed:.2f}s, expected < 1s"


@pytest.mark.asyncio
async def test_worker_recover_moves_stale_running_back_to_pending(env) -> None:
    """Rows left 'running' by a crashed previous process get re-queued."""

    first_raw = await _persist_raw(env, payload_hash="a", message_id="m-1")
    row, _ = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=first_raw.id,
    )
    # Simulate a previous worker that claimed and crashed.
    await env.outbox_repo.claim_next_pending()
    assert (await env.outbox_repo.get_by_id(row.id)).status == "running"

    # Reclaim the same row via recover(), as the next process start would.
    worker = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=lambda m: asyncio.sleep(0),
        max_concurrent=1,
        num_workers=1,
        poll_interval_seconds=0.05,
    )
    recovered = await worker.recover()
    assert recovered == 1
    assert (await env.outbox_repo.get_by_id(row.id)).status == "pending"
    await worker.stop()


@pytest.mark.asyncio
async def test_duplicate_trigger_does_not_create_second_outbox_row(env) -> None:
    """The UNIQUE constraint prevents two rows for the same trigger."""

    first_raw = await _persist_raw(env, payload_hash="a", message_id="m-1")
    first, created1 = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=first_raw.id,
    )
    second, created2 = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=first_raw.id,
    )
    assert created1 is True
    assert created2 is False
    assert first.id == second.id
    stats = await env.outbox_repo.stats()
    assert stats["pending"] == 1
