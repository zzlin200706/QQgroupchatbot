"""End-to-end tests for the interaction outbox state machine.

Each test drives the actual state transitions through the public
APIs (``create_pending`` / ``claim_next_pending`` / ``mark_completed``
/ ``mark_failed`` / ``requeue_for_retry`` / ``recover_stale_running``)
rather than poking at SQL.  This is the contract that the worker
relies on, so a regression here is the difference between "the bot
recovers from a crash" and "the bot silently loses a reply".
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


def _valid_payload(message_id: str = "m-1") -> dict:
    return {
        "id": "event-1",
        "op": 0,
        "s": 1,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": message_id,
            "timestamp": "2026-08-11T12:00:00+08:00",
            "group_openid": "g-1",
            "message_type": 0,
            "author": {"id": "user-1", "member_openid": "user-1"},
            "content": "hi",
        },
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


def _message() -> InternalMessage:
    actor = _identity()
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id="m-1",
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


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    await db.initialize()
    outbox_repo = InteractionOutboxRepository(db.session_factory)
    raw_event_repo = RawEventRepository(db.session_factory)
    yield type("Env", (), {
        "database": db,
        "outbox_repo": outbox_repo,
        "raw_event_repo": raw_event_repo,
    })()
    await db.dispose()


async def _create_row(env, *, payload_hash: str, message_id: str) -> RawEvent:
    raw = RawEvent(
        platform="qq_official",
        received_at=datetime.now(timezone.utc),
        raw_payload=_valid_payload(message_id=message_id),
        payload_hash=payload_hash,
    )
    return await env.raw_event_repo.insert(raw)


# --- Normal lifecycle: pending -> running -> completed ---


@pytest.mark.asyncio
async def test_normal_lifecycle_pending_running_completed(env) -> None:
    raw = await _create_row(env, payload_hash="a", message_id="m-1")
    row, _ = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
    )
    assert row.status == "pending"
    assert row.attempt_count == 0

    claimed = await env.outbox_repo.claim_next_pending()
    assert claimed is not None
    assert claimed.id == row.id
    assert claimed.status == "running"
    assert claimed.attempt_count == 1

    completed = await env.outbox_repo.mark_completed(
        row.id, response_message_id="qq-msg-1"
    )
    assert completed is not None
    assert completed.status == "completed"
    assert completed.response_message_id == "qq-msg-1"
    assert completed.completed_at is not None


# --- Provider failure: pending -> running -> pending (retry) ---


@pytest.mark.asyncio
async def test_retryable_failure_returns_row_to_pending(env) -> None:
    raw = await _create_row(env, payload_hash="a", message_id="m-1")
    row, _ = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
        max_attempts=3,
    )

    # Attempt 1 fails, attempt_count was 1 (from claim), requeue.
    await env.outbox_repo.claim_next_pending()
    requeued = await env.outbox_repo.requeue_for_retry(
        row.id, reason="provider 500"
    )
    assert requeued.status == "pending"
    assert requeued.attempt_count == 1  # unchanged by requeue
    assert "provider 500" in (requeued.last_error or "")

    # Attempt 2 fails too.
    await env.outbox_repo.claim_next_pending()
    requeued = await env.outbox_repo.requeue_for_retry(
        row.id, reason="provider 500 again"
    )
    assert requeued.status == "pending"

    # Attempt 3: claim brings attempt_count to 3; requeue at max -> failed.
    claimed = await env.outbox_repo.claim_next_pending()
    assert claimed.attempt_count == 3
    requeued = await env.outbox_repo.requeue_for_retry(
        row.id, reason="provider 500 final"
    )
    assert requeued.status == "failed"
    assert "retry limit reached" in (requeued.last_error or "")


# --- SIGTERM / crash recovery ---


@pytest.mark.asyncio
async def test_stale_running_is_recovered_on_startup(env) -> None:
    raw = await _create_row(env, payload_hash="a", message_id="m-1")
    row, _ = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
    )

    # Simulate a previous process that claimed then crashed.
    claimed = await env.outbox_repo.claim_next_pending()
    assert claimed.status == "running"

    recovered = await env.outbox_repo.recover_stale_running()
    assert recovered == 1
    refreshed = await env.outbox_repo.get_by_id(row.id)
    assert refreshed is not None
    assert refreshed.status == "pending"
    # The recover step does not increment attempt_count; the next
    # claim will.
    assert refreshed.attempt_count == 1


@pytest.mark.asyncio
async def test_recovered_row_is_dispatched_by_fresh_worker(env) -> None:
    """Full SIGTERM simulation: claim -> crash -> restart -> success."""

    raw = await _create_row(env, payload_hash="a", message_id="m-1")
    row, _ = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
    )

    # Phase 1: process A claims and "crashes" (we leave the row running).
    await env.outbox_repo.claim_next_pending()
    assert (await env.outbox_repo.get_by_id(row.id)).status == "running"

    # Phase 2: process B starts, recovers the row, dispatches it.
    handle_calls: list[InternalMessage] = []

    async def handle(message: InternalMessage) -> None:
        handle_calls.append(message)

    worker = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=handle,
        max_concurrent=2,
        num_workers=1,
        poll_interval_seconds=0.05,
    )
    await worker.start()
    try:
        # Wait for the row to be claimed and completed.
        for _ in range(50):
            refreshed = await env.outbox_repo.get_by_id(row.id)
            if refreshed.status == "completed":
                break
            await asyncio.sleep(0.05)
        assert refreshed.status == "completed"
    finally:
        await worker.stop()

    assert len(handle_calls) == 1
    assert handle_calls[0].platform_message_id == "m-1"


# --- Duplicate event ---


@pytest.mark.asyncio
async def test_duplicate_trigger_creates_only_one_outbox_row(env) -> None:
    raw = await _create_row(env, payload_hash="a", message_id="m-1")
    first, created1 = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
    )
    second, created2 = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
    )
    assert created1 is True
    assert created2 is False
    assert first.id == second.id


# --- Already completed: restart does not re-send ---


@pytest.mark.asyncio
async def test_completed_row_is_not_redispatched(env) -> None:
    raw = await _create_row(env, payload_hash="a", message_id="m-1")
    row, _ = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
    )
    await env.outbox_repo.claim_next_pending()
    await env.outbox_repo.mark_completed(row.id, response_message_id="r-1")

    # A second worker starts and should find nothing to do.
    handle_calls: list[InternalMessage] = []

    async def handle(message: InternalMessage) -> None:
        handle_calls.append(message)

    worker = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=handle,
        max_concurrent=2,
        num_workers=1,
        poll_interval_seconds=0.05,
    )
    await worker.start()
    # Give the worker a chance to look for work.
    await asyncio.sleep(0.3)
    await worker.stop()

    assert handle_calls == []


# --- Retry limit ---


@pytest.mark.asyncio
async def test_max_attempts_terminates_at_failed_state(env) -> None:
    raw = await _create_row(env, payload_hash="a", message_id="m-1")
    row, _ = await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
        max_attempts=2,
    )

    # First failure.
    await env.outbox_repo.claim_next_pending()
    r = await env.outbox_repo.requeue_for_retry(row.id, reason="fail-1")
    assert r.status == "pending"

    # Second attempt fails; attempt_count was 2, == max_attempts, -> failed.
    await env.outbox_repo.claim_next_pending()
    r = await env.outbox_repo.requeue_for_retry(row.id, reason="fail-2")
    assert r.status == "failed"

    # A subsequent worker must not pick it up.
    handle_calls: list[InternalMessage] = []

    async def handle(message: InternalMessage) -> None:
        handle_calls.append(message)

    worker = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=handle,
        max_concurrent=2,
        num_workers=1,
        poll_interval_seconds=0.05,
    )
    await worker.start()
    await asyncio.sleep(0.3)
    await worker.stop()
    assert handle_calls == []


# --- Concurrent recovery: multiple pending rows ---


@pytest.mark.asyncio
async def test_multiple_pending_rows_drain_with_concurrency_limit(env) -> None:
    """A burst of pending rows drains in parallel up to max_concurrent."""

    for index in range(6):
        raw = await _create_row(
            env, payload_hash=f"hash-{index}", message_id=f"m-{index}"
        )
        await env.outbox_repo.create_pending(
            platform="qq_official",
            group_id="g-1",
            trigger_message_id=f"m-{index}",
            interaction_name="ping",
            source_raw_event_id=raw.id,
        )

    concurrent_now = 0
    peak_concurrent = 0
    counter_lock = asyncio.Lock()

    async def handle(message: InternalMessage) -> None:
        nonlocal concurrent_now, peak_concurrent
        async with counter_lock:
            concurrent_now += 1
            peak_concurrent = max(peak_concurrent, concurrent_now)
        # Brief sleep so the worker can pull the next row while
        # this one is still in flight.  0.05s is enough to observe
        # overlap without making the test slow.
        await asyncio.sleep(0.05)
        async with counter_lock:
            concurrent_now -= 1

    worker = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=handle,
        max_concurrent=2,
        num_workers=1,
        poll_interval_seconds=0.02,
    )
    await worker.start()
    try:
        # Wait for everything to drain.
        for _ in range(200):
            stats = await env.outbox_repo.stats()
            if stats["pending"] == 0 and stats["running"] == 0:
                break
            await asyncio.sleep(0.05)
    finally:
        await worker.stop()

    # The peak concurrency must never have exceeded max_concurrent.
    assert peak_concurrent <= 2
    stats = await env.outbox_repo.stats()
    assert stats["completed"] == 6
    assert stats["pending"] == 0
    assert stats["failed"] == 0
