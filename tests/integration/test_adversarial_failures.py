"""Adversarial failure tests for the outbox pipeline.

Each test injects one specific failure mode and verifies that:

  * the bot does not crash;
  * a single request failure does not affect other requests;
  * the outbox state machine transitions to a sensible terminal
    state ('completed' or 'failed');
  * on a simulated crash + restart, the next process can recover
    cleanly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable

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
from app.llm.providers.base import (
    LLMAuthenticationError,
    LLMProviderError,
    LLMTimeoutError,
)
from app.services.interaction_outbox_worker import InteractionOutboxWorker
from app.services.command_status import CommandStatus
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
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


def _message(message_id: str = "m-1") -> InternalMessage:
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


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'adv.db'}")
    await db.initialize()
    yield type("Env", (), {
        "database": db,
        "outbox_repo": InteractionOutboxRepository(db.session_factory),
        "raw_event_repo": RawEventRepository(db.session_factory),
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


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]], *, timeout: float = 5.0
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("predicate did not become true within timeout")


def _build_worker(
    env,
    handle,
    *,
    max_concurrent: int = 2,
) -> InteractionOutboxWorker:
    return InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=handle,
        max_concurrent=max_concurrent,
        num_workers=1,
        poll_interval_seconds=0.02,
    )


# --- Provider failures ---


@pytest.mark.asyncio
async def test_unhandled_provider_timeout_is_not_blindly_retried(env) -> None:
    raw = await _create_row(env, payload_hash="a", message_id="m-1")
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
        max_attempts=3,
    )
    call_count = 0

    async def handle(m):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise LLMTimeoutError("simulated timeout")
        return CommandStatus.SUCCEEDED

    worker = _build_worker(env, handle)
    await worker.start()
    try:
        async def is_failed() -> bool:
            stats = await env.outbox_repo.stats()
            return stats["failed"] == 1

        await _wait_until(is_failed, timeout=5.0)
    finally:
        await worker.stop()
    assert call_count == 1
    stats = await env.outbox_repo.stats()
    assert stats["failed"] == 1
    assert stats["pending"] == 0


@pytest.mark.asyncio
async def test_provider_auth_error_fails_immediately(env) -> None:
    """Auth errors are not retried; they are configuration bugs."""

    raw = await _create_row(env, payload_hash="a", message_id="m-1")
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
        max_attempts=3,
    )
    call_count = 0

    async def handle(m):
        nonlocal call_count
        call_count += 1
        raise LLMAuthenticationError("bad api key")

    worker = _build_worker(env, handle)
    await worker.start()
    try:
        async def is_failed() -> bool:
            stats = await env.outbox_repo.stats()
            return stats["failed"] == 1

        await _wait_until(is_failed, timeout=5.0)
    finally:
        await worker.stop()
    assert call_count == 1
    stats = await env.outbox_repo.stats()
    assert stats["failed"] == 1


# --- Concurrency: one bad request does not affect siblings ---


@pytest.mark.asyncio
async def test_one_failing_request_does_not_block_others(env) -> None:
    """A poison message does not stop the rest of the queue."""

    for i in range(5):
        raw = await _create_row(env, payload_hash=f"h-{i}", message_id=f"m-{i}")
        await env.outbox_repo.create_pending(
            platform="qq_official",
            group_id="g-1",
            trigger_message_id=f"m-{i}",
            interaction_name="ping",
            source_raw_event_id=raw.id,
            max_attempts=1,  # one-shot so the test is fast
        )

    seen: list[str] = []

    async def handle(m):
        seen.append(m.platform_message_id or "")
        if m.platform_message_id == "m-2":
            raise LLMProviderError("poison")
        return CommandStatus.SUCCEEDED

    worker = _build_worker(env, handle, max_concurrent=2)
    await worker.start()
    try:
        async def is_settled() -> bool:
            stats = await env.outbox_repo.stats()
            return stats["pending"] == 0 and stats["running"] == 0

        await _wait_until(is_settled, timeout=5.0)
    finally:
        await worker.stop()

    # All 5 were processed; m-2 failed, the others completed.
    assert sorted(seen) == ["m-0", "m-1", "m-2", "m-3", "m-4"]
    stats = await env.outbox_repo.stats()
    assert stats["completed"] == 4
    assert stats["failed"] == 1


# --- Malformed QQ event ---


@pytest.mark.asyncio
async def test_unparseable_raw_event_fails_the_outbox_row(env) -> None:
    raw = RawEvent(
        platform="qq_official",
        received_at=datetime.now(timezone.utc),
        raw_payload={"unparseable": True},  # no 'd' key
        payload_hash="a",
    )
    await env.raw_event_repo.insert(raw)
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
    )

    handle_calls: list[str] = []

    async def handle(m):
        handle_calls.append(m.platform_message_id or "")

    worker = _build_worker(env, handle)
    await worker.start()
    try:
        async def is_settled() -> bool:
            stats = await env.outbox_repo.stats()
            return stats["pending"] == 0 and stats["running"] == 0

        await _wait_until(is_settled, timeout=5.0)
    finally:
        await worker.stop()

    # _load_message returned None -> mark_failed; handle never ran.
    stats = await env.outbox_repo.stats()
    assert stats["failed"] == 1
    assert stats["completed"] == 0
    assert handle_calls == []


# --- Mid-flight crash + restart ---


@pytest.mark.asyncio
async def test_mid_flight_crash_then_restart_completes(env) -> None:
    """Worker A claims and 'crashes' (we never mark anything)."""

    for i in range(3):
        raw = await _create_row(env, payload_hash=f"h-{i}", message_id=f"m-{i}")
        await env.outbox_repo.create_pending(
            platform="qq_official",
            group_id="g-1",
            trigger_message_id=f"m-{i}",
            interaction_name="ping",
            source_raw_event_id=raw.id,
        )

    # Phase 1: worker A claims m-0, m-1, m-2 then "dies".  Three
    # worker tasks so all three rows are claimed concurrently.
    async def stalling_handle(m):
        await asyncio.Event().wait()  # never returns
        return None

    worker_a = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=stalling_handle,
        max_concurrent=3,
        num_workers=3,
        poll_interval_seconds=0.02,
    )
    await worker_a.start()
    # Give the workers a moment to claim all 3 rows.  Three
    # concurrent worker tasks racing on the same DB will
    # serialise through SQLite, so we wait until the stats show
    # 3 running or a short deadline expires.
    for _ in range(50):
        stats_a = await env.outbox_repo.stats()
        if stats_a["running"] == 3:
            break
        await asyncio.sleep(0.05)
    assert stats_a["running"] == 3, stats_a
    # Crash.
    await worker_a.stop()

    # Phase 2: worker B starts, recovers, and finishes them.
    handle_b_calls: list[str] = []

    async def finishing_handle(m):
        handle_b_calls.append(m.platform_message_id or "")
        return CommandStatus.SUCCEEDED

    worker_b = InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_event_repo,
        interaction_repository=None,
        on_handle=finishing_handle,
        max_concurrent=3,
        num_workers=3,
        poll_interval_seconds=0.02,
    )
    await worker_b.start()
    try:
        async def is_settled() -> bool:
            stats = await env.outbox_repo.stats()
            return stats["pending"] == 0 and stats["running"] == 0

        await _wait_until(is_settled, timeout=5.0)
    finally:
        await worker_b.stop()

    assert sorted(handle_b_calls) == ["m-0", "m-1", "m-2"]
    stats = await env.outbox_repo.stats()
    assert stats["completed"] == 3
    assert stats["failed"] == 0


# --- Concurrent shutdown ---


@pytest.mark.asyncio
async def test_double_stop_is_idempotent(env) -> None:
    for i in range(3):
        raw = await _create_row(env, payload_hash=f"h-{i}", message_id=f"m-{i}")
        await env.outbox_repo.create_pending(
            platform="qq_official",
            group_id="g-1",
            trigger_message_id=f"m-{i}",
            interaction_name="ping",
            source_raw_event_id=raw.id,
        )

    async def handle(m):
        return None

    worker = _build_worker(env, handle)
    await worker.start()
    await asyncio.sleep(0.2)

    # Calling stop twice in quick succession must not raise.
    await worker.stop()
    await worker.stop()
    # start() again is also a no-op.
    await worker.start()
    await worker.stop()
