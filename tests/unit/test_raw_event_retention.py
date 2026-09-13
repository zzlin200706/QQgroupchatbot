"""Tests for ``raw_events`` retention.

The retention policy: a raw_event is pruned when it is

  * older than the configured retention window;
  * not referenced by any row in ``messages`` (the parsed
    source of truth for context queries);
  * not referenced by any ``interaction_outbox`` row in
    ``pending`` or ``running`` state (the worker re-parses
    the raw payload during crash recovery).

The tests below exercise every branch of that policy plus
restart-after-prune correctness.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

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
from app.storage.message_repository import MessageRepository
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository
from app.parsers import QQOfficialMessageParser
from app.services.normalized_message_ingestion import (
    QQ_OFFICIAL_PARSER_NAME,
    QQ_OFFICIAL_PARSER_VERSION,
    QQOfficialNormalizedMessageIngestionService,
)


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


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}")
    await db.initialize()
    yield type("Env", (), {
        "database": db,
        "raw_repo": RawEventRepository(db.session_factory),
        "outbox_repo": InteractionOutboxRepository(db.session_factory),
        "message_repo": MessageRepository(db.session_factory),
    })()
    await db.dispose()


async def _make_old(env, *, payload_hash: str, message_id: str) -> RawEvent:
    raw = RawEvent(
        platform="qq_official",
        received_at=datetime.now(timezone.utc) - timedelta(days=60),
        raw_payload=_valid_payload(message_id=message_id),
        payload_hash=payload_hash,
    )
    return await env.raw_repo.insert(raw)


async def _make_fresh(env, *, payload_hash: str, message_id: str) -> RawEvent:
    raw = RawEvent(
        platform="qq_official",
        received_at=datetime.now(timezone.utc),
        raw_payload=_valid_payload(message_id=message_id),
        payload_hash=payload_hash,
    )
    return await env.raw_repo.insert(raw)


def _worker(env) -> InteractionOutboxWorker:
    return InteractionOutboxWorker(
        session_factory=env.database.session_factory,
        outbox_repository=env.outbox_repo,
        raw_event_repository=env.raw_repo,
        interaction_repository=None,
        on_handle=lambda m: asyncio.sleep(0),
        max_concurrent=2,
        num_workers=1,
        poll_interval_seconds=0.05,
    )


# --- Unused old raw events are pruned ---


@pytest.mark.asyncio
async def test_old_unused_raw_event_is_pruned(env) -> None:
    raw = await _make_old(env, payload_hash="a", message_id="m-1")
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = await env.raw_repo.prune_older_than(older_than=cutoff)
    assert deleted == 1
    assert await env.raw_repo.get_by_id(raw.id) is None


# --- Fresh raw events are preserved ---


@pytest.mark.asyncio
async def test_fresh_raw_event_is_preserved(env) -> None:
    raw = await _make_fresh(env, payload_hash="a", message_id="m-1")
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = await env.raw_repo.prune_older_than(older_than=cutoff)
    assert deleted == 0
    assert await env.raw_repo.get_by_id(raw.id) is not None


# --- A raw event referenced by messages is preserved ---


@pytest.mark.asyncio
async def test_raw_event_referenced_by_messages_is_preserved(env) -> None:
    raw = await _make_old(env, payload_hash="a", message_id="m-1")
    # Persist a normalized message that references the raw_event.
    normalized_ingestion = QQOfficialNormalizedMessageIngestionService(
        repository=env.message_repo,
        parser=QQOfficialMessageParser(),
    )
    await normalized_ingestion.ingest(raw)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = await env.raw_repo.prune_older_than(older_than=cutoff)
    # The FK constraint prevents the delete; the chunk loop
    # completes with zero rows deleted.
    assert deleted == 0
    assert await env.raw_repo.get_by_id(raw.id) is not None


# --- A raw event referenced by a pending outbox row is preserved ---


@pytest.mark.asyncio
async def test_raw_event_referenced_by_pending_outbox_is_preserved(env) -> None:
    raw = await _make_old(env, payload_hash="a", message_id="m-1")
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = await env.raw_repo.prune_older_than(older_than=cutoff)
    assert deleted == 0
    assert await env.raw_repo.get_by_id(raw.id) is not None


# --- A raw event referenced by a running outbox row is preserved ---


@pytest.mark.asyncio
async def test_raw_event_referenced_by_running_outbox_is_preserved(env) -> None:
    raw = await _make_old(env, payload_hash="a", message_id="m-1")
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
    )
    await env.outbox_repo.claim_next_pending()
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = await env.raw_repo.prune_older_than(older_than=cutoff)
    assert deleted == 0
    assert await env.raw_repo.get_by_id(raw.id) is not None


# --- A raw event with a completed outbox row IS pruned (no live ref) ---


@pytest.mark.asyncio
async def test_old_raw_event_with_completed_outbox_is_pruned(env) -> None:
    raw = await _make_old(env, payload_hash="a", message_id="m-1")
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-1",
        interaction_name="ping",
        source_raw_event_id=raw.id,
    )
    # The interaction completed long ago.
    claimed = await env.outbox_repo.claim_next_pending()
    assert claimed is not None
    await env.outbox_repo.mark_completed(claimed.id, response_message_id="r")
    # The terminal prune runs in the worker first, but we can
    # also call the raw prune directly.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = await env.raw_repo.prune_older_than(older_than=cutoff)
    assert deleted == 1
    assert await env.raw_repo.get_by_id(raw.id) is None


# --- Chunked pruning ---


@pytest.mark.asyncio
async def test_chunked_pruning_processes_all_rows(env) -> None:
    for i in range(25):
        await _make_old(
            env, payload_hash=f"h-{i}", message_id=f"m-{i}"
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = await env.raw_repo.prune_older_than(
        older_than=cutoff, chunk_size=10
    )
    assert deleted == 25


# --- Restart after prune still finds the remaining rows ---


@pytest.mark.asyncio
async def test_restart_after_prune_keeps_remaining_rows(env) -> None:
    # Two old + one fresh.
    for i in range(2):
        await _make_old(
            env, payload_hash=f"old-{i}", message_id=f"old-m-{i}"
        )
    fresh = await _make_fresh(
        env, payload_hash="fresh", message_id="fresh-m"
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = await env.raw_repo.prune_older_than(older_than=cutoff)
    assert deleted == 2

    # The fresh row is still there; old rows are gone.
    assert await env.raw_repo.get_by_id(fresh.id) is not None
    recent = await env.raw_repo.list_recent(limit=10)
    assert recent[0].id == fresh.id


# --- Duplicate detection remains correct after prune ---


@pytest.mark.asyncio
async def test_duplicate_detection_remains_after_prune(env) -> None:
    raw = await _make_old(env, payload_hash="dup", message_id="m-1")
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = await env.raw_repo.prune_older_than(older_than=cutoff)
    assert deleted == 1
    # Re-inserting the same payload_hash must succeed (no UNIQUE
    # violation against a now-gone row).  SQLite reuses the
    # autoincrement id after a DELETE, so we assert the row was
    # re-inserted by checking it exists with the right hash.
    reinserted = await _make_old(
        env, payload_hash="dup", message_id="m-1"
    )
    assert reinserted.payload_hash == raw.payload_hash
    found = await env.raw_repo.get_by_payload_hash("qq_official", "dup")
    assert found is not None
    assert found.id == reinserted.id


# --- Worker prune_raw_events uses the same policy ---


@pytest.mark.asyncio
async def test_worker_prune_raw_events_uses_repository(env) -> None:
    await _make_old(env, payload_hash="a", message_id="m-1")
    worker = _worker(env)
    deleted = await worker.prune_raw_events(retention_days=30, chunk_size=10)
    assert deleted == 1
    deleted_again = await worker.prune_raw_events(
        retention_days=30, chunk_size=10
    )
    assert deleted_again == 0


# --- Mixed: some old, some fresh, some referenced ---


@pytest.mark.asyncio
async def test_prune_handles_mixed_set_correctly(env) -> None:
    # 1: old + unreferenced -> pruned
    await _make_old(env, payload_hash="a", message_id="m-1")
    # 2: old + referenced by messages -> preserved
    msg_ref = await _make_old(env, payload_hash="b", message_id="m-2")
    await QQOfficialNormalizedMessageIngestionService(
        repository=env.message_repo,
        parser=QQOfficialMessageParser(),
    ).ingest(msg_ref)
    # 3: old + pending outbox -> preserved
    pending_ref = await _make_old(env, payload_hash="c", message_id="m-3")
    await env.outbox_repo.create_pending(
        platform="qq_official",
        group_id="g-1",
        trigger_message_id="m-3",
        interaction_name="ping",
        source_raw_event_id=pending_ref.id,
    )
    # 4: fresh -> preserved
    await _make_fresh(env, payload_hash="d", message_id="m-4")

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = await env.raw_repo.prune_older_than(older_than=cutoff)
    assert deleted == 1

    # Only the unreferenced old row is gone.
    assert await env.raw_repo.get_by_payload_hash("qq_official", "a") is None
    assert await env.raw_repo.get_by_payload_hash("qq_official", "b") is not None
    assert await env.raw_repo.get_by_payload_hash("qq_official", "c") is not None
    assert await env.raw_repo.get_by_payload_hash("qq_official", "d") is not None
