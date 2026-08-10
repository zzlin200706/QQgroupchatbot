from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import websockets
from sqlalchemy import func, select

from app.config import Settings
from app.domain.messages import (
    ForwardResolutionStatus,
    ForwardSegment,
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
    ProvenanceSource,
    ReplyResolutionStatus,
    ReplySegment,
    ResolvedMessageReference,
    TextSegment,
    UnknownSegment,
)
from app.main import create_app
from app.parsers import OneBotMessageParser, QQOfficialMessageParser
from app.services.normalized_message_ingestion import NormalizedMessageIngestionService
from app.services.raw_event_ingestion import RawEventIngestionService
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.models import MessageNodeRecord, MessageRecord, RawEvent
from app.storage.raw_event_repository import RawEventRepository


ONEBOT_FIXTURE = Path(__file__).parents[1] / "fixtures" / "onebot" / "real_group_text_sanitized.json"
QQ_SAMPLES = Path(__file__).parents[2] / "data" / "qq_official_samples"


async def create_storage(
    tmp_path: Path,
) -> tuple[Database, RawEventRepository, MessageRepository]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'normalized.db'}")
    await database.initialize()
    return (
        database,
        RawEventRepository(database.session_factory),
        MessageRepository(database.session_factory),
    )


async def insert_raw(
    repository: RawEventRepository,
    payload: dict[str, Any],
    *,
    platform: str,
) -> RawEvent:
    return await repository.insert(
        RawEvent(
            platform=platform,
            received_at=datetime.now(timezone.utc),
            event_time=None,
            post_type=None,
            message_type=None,
            sub_type=None,
            self_id=None,
            user_id=None,
            group_id=None,
            message_id=None,
            raw_payload=payload,
            payload_hash=RawEventIngestionService.payload_hash(payload),
        )
    )


async def counts(database: Database) -> tuple[int, int, int]:
    async with database.session_factory() as session:
        raw_count = await session.scalar(select(func.count()).select_from(RawEvent))
        message_count = await session.scalar(select(func.count()).select_from(MessageRecord))
        node_count = await session.scalar(select(func.count()).select_from(MessageNodeRecord))
    return int(raw_count or 0), int(message_count or 0), int(node_count or 0)


@pytest.mark.asyncio
async def test_real_onebot_fixture_round_trips_strictly(tmp_path: Path) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    payload = json.loads(ONEBOT_FIXTURE.read_text(encoding="utf-8"))
    try:
        raw = await insert_raw(raw_repository, payload, platform="onebot11")
        original = OneBotMessageParser().parse(payload, source_raw_event_id=raw.id)
        assert original is not None

        await message_repository.persist(
            original,
            parser_name="onebot_message_parser",
            parser_version="1",
        )
        loaded = await message_repository.get_by_raw_event_id(raw.id)

        assert loaded == original
        async with database.session_factory() as session:
            normalized_id = await session.scalar(select(MessageRecord.id))
            node_count = await session.scalar(
                select(func.count()).select_from(MessageNodeRecord)
            )
        assert normalized_id is not None
        assert await message_repository.get_by_id(normalized_id) == original
        print(
            f"raw_event_id={raw.id} normalized_message_id={normalized_id} "
            f"node_count={node_count} roundtrip_ok={loaded == original}"
        )
    finally:
        await database.dispose()


def nested_forward_event() -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 3001,
        "group_id": 2001,
        "user_id": 1001,
        "time": 1700000000,
        "sender": {"nickname": "Outer", "card": "Outer card"},
        "message": [
            {
                "type": "forward",
                "data": {
                    "id": "outer-forward",
                    "content": [
                        {"type": "text", "data": {"text": "forward loose content"}},
                        {
                            "type": "node",
                            "data": {
                                "id": "outer-node",
                                "content": [
                                    {"type": "future_type", "data": {"kept": [1, 2]}},
                                    {
                                        "type": "forward",
                                        "data": {
                                            "id": "nested-forward",
                                            "content": [
                                                {
                                                    "type": "node",
                                                    "data": {
                                                        "id": "nested-node",
                                                        "content": [
                                                            {"type": "text", "data": {"text": "deep"}}
                                                        ],
                                                    },
                                                }
                                            ],
                                        },
                                    },
                                ],
                            },
                        },
                    ],
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_nested_forward_relations_sender_and_provenance_round_trip(tmp_path: Path) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    payload = nested_forward_event()
    try:
        raw = await insert_raw(raw_repository, payload, platform="onebot11")
        original = OneBotMessageParser().parse(payload, source_raw_event_id=raw.id)
        assert original is not None
        await message_repository.persist(original, parser_name="onebot_message_parser", parser_version="1")
        loaded = await message_repository.get_by_raw_event_id(raw.id)
        assert loaded == original
        assert loaded is not None

        outer = loaded.segments[0]
        assert isinstance(outer, ForwardSegment)
        assert len(outer.content) == 1
        assert isinstance(outer.content[0], TextSegment)
        assert len(outer.nodes) == 1
        outer_node = outer.nodes[0]
        assert outer_node.sender.user_id is None
        assert outer_node.sender.availability is IdentityAvailability.UNAVAILABLE
        assert outer_node.sender != loaded.author
        assert outer_node.provenance.source_type is ProvenanceSource.FORWARD_NODE
        nested = outer_node.content[1]
        assert isinstance(nested, ForwardSegment)
        assert nested.nodes[0].provenance.forward_depth == 2

        async with database.session_factory() as session:
            rows = (
                await session.scalars(
                    select(MessageNodeRecord).order_by(MessageNodeRecord.id)
                )
            ).all()
        assert any(row.relation == "content" and row.node_kind == "text" for row in rows)
        assert any(row.relation == "nodes" and row.node_kind == "forward_node" for row in rows)
        assert max(row.depth for row in rows) == 4
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_resolved_reply_reference_round_trips_as_its_own_relation(tmp_path: Path) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    payload = {
        "post_type": "message",
        "message_id": 2,
        "user_id": 1,
        "time": 1700000000,
        "message": [{"type": "reply", "data": {"id": 1}}],
    }
    try:
        raw = await insert_raw(raw_repository, payload, platform="onebot11")
        parsed = OneBotMessageParser().parse(payload, source_raw_event_id=raw.id)
        assert parsed is not None
        reply = parsed.segments[0]
        assert isinstance(reply, ReplySegment)
        resolved = ResolvedMessageReference(
            platform_message_id="1",
            author=IdentityRef(
                platform="onebot11",
                user_id=None,
                display_name=None,
                card=None,
                source=IdentitySource.UNKNOWN,
                availability=IdentityAvailability.UNAVAILABLE,
            ),
            timestamp=datetime(2023, 1, 1, tzinfo=timezone.utc),
            segments=(TextSegment(position=0, raw_data={"text": "quoted"}, text="quoted"),),
            raw_data={"message_id": 1, "future": {"kept": True}},
        )
        enriched_reply = replace(
            reply,
            resolution_status=ReplyResolutionStatus.RESOLVED,
            resolved_message=resolved,
            resolved_raw_data={"message_id": 1, "status": "ok"},
        )
        original = replace(parsed, segments=(enriched_reply,))

        await message_repository.persist(original, parser_name="onebot_enriched", parser_version="1")
        loaded = await message_repository.get_by_raw_event_id(raw.id)
        assert loaded == original

        async with database.session_factory() as session:
            relation = await session.scalar(
                select(MessageNodeRecord.relation).where(
                    MessageNodeRecord.node_kind == "resolved_message"
                )
            )
        assert relation == "resolved_message"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_unknown_segment_and_all_segment_positions_round_trip(tmp_path: Path) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    payload = {
        "post_type": "message",
        "message_id": 3,
        "user_id": 1,
        "message": [
            {"type": "text", "data": {"text": "first"}},
            {"type": "at", "data": {"qq": "2"}},
            {"type": "text", "data": {"text": "last"}},
            {"type": "image", "data": {"file": "image", "future": 1}},
            {"type": "future_type", "data": {"nested": [1, {"x": True}]}},
        ],
    }
    try:
        raw = await insert_raw(raw_repository, payload, platform="onebot11")
        original = OneBotMessageParser().parse(payload, source_raw_event_id=raw.id)
        assert original is not None
        await message_repository.persist(original, parser_name="onebot_message_parser", parser_version="1")
        loaded = await message_repository.get_by_raw_event_id(raw.id)
        assert loaded == original
        assert loaded is not None
        assert [segment.position for segment in loaded.segments] == [0, 1, 2, 3, 4]
        assert isinstance(loaded.segments[-1], UnknownSegment)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_idempotency_parser_replay_and_duplicate_receipts(tmp_path: Path) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    payload = json.loads(ONEBOT_FIXTURE.read_text(encoding="utf-8"))
    try:
        raw_one = await insert_raw(raw_repository, payload, platform="onebot11")
        raw_two = await insert_raw(raw_repository, payload, platform="onebot11")
        message_one = OneBotMessageParser().parse(payload, source_raw_event_id=raw_one.id)
        message_two = OneBotMessageParser().parse(payload, source_raw_event_id=raw_two.id)
        assert message_one is not None and message_two is not None

        await message_repository.persist(message_one, parser_name="parser", parser_version="1")
        await message_repository.persist(message_one, parser_name="parser", parser_version="1")
        await message_repository.persist(message_one, parser_name="parser", parser_version="2")
        await message_repository.persist(message_two, parser_name="parser", parser_version="1")

        assert await counts(database) == (2, 3, 3)
        async with database.session_factory() as session:
            versions = set((await session.scalars(select(MessageRecord.parser_version))).all())
        assert versions == {"1", "2"}
        assert await message_repository.get_by_raw_event_id(
            raw_one.id, parser_name="parser", parser_version="1"
        ) == message_one
        assert await message_repository.get_by_raw_event_id(
            raw_one.id, parser_name="parser", parser_version="2"
        ) == message_one
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_non_message_raw_receipt_is_kept_without_a_normalized_message(tmp_path: Path) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    try:
        raw = await insert_raw(
            raw_repository,
            {"post_type": "notice", "notice_type": "group_upload"},
            platform="onebot11",
        )
        service = NormalizedMessageIngestionService(
            repository=message_repository,
            parser=OneBotMessageParser(),
        )

        assert await service.ingest(raw) is None
        assert await counts(database) == (1, 0, 0)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_live_onebot_pipeline_commits_raw_before_normalized_message(tmp_path: Path) -> None:
    payload = json.loads(ONEBOT_FIXTURE.read_text(encoding="utf-8"))
    event_sent = asyncio.Event()

    async def handler(connection: websockets.ServerConnection) -> None:
        await connection.send(json.dumps(payload))
        event_sent.set()
        await connection.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        application = create_app(
            Settings(
                app_env="test",
                onebot_ws_url=f"ws://127.0.0.1:{port}",
                database_url=f"sqlite+aiosqlite:///{tmp_path / 'live-pipeline.db'}",
                onebot_reconnect_initial_delay_seconds=0.01,
                onebot_reconnect_max_delay_seconds=0.02,
            )
        )
        async with application.router.lifespan_context(application):
            await asyncio.wait_for(event_sent.wait(), timeout=1)

            async def normalized_is_available() -> bool:
                receipts = await application.state.raw_event_repository.list_recent()
                if len(receipts) != 1:
                    return False
                normalized = await application.state.message_repository.get_by_raw_event_id(
                    receipts[0].id
                )
                return normalized is not None

            async def wait_until_normalized() -> None:
                while not await normalized_is_available():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_until_normalized(), timeout=1)
            receipts = await application.state.raw_event_repository.list_recent()
            normalized = await application.state.message_repository.get_by_raw_event_id(
                receipts[0].id
            )
            assert normalized is not None
            assert normalized.source_raw_event_id == receipts[0].id
            assert normalized.platform_message_id == str(payload["message_id"])


@pytest.mark.parametrize(
    "filename",
    [
        "001_text.json",
        "002_at_bot.json",
        "004_image.json",
        "009_file.json",
        "007_reply_text.json",
        "012_merged_forward.json",
    ],
)
@pytest.mark.asyncio
async def test_qq_official_samples_round_trip_strictly(tmp_path: Path, filename: str) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    payload = json.loads((QQ_SAMPLES / filename).read_text(encoding="utf-8"))
    try:
        raw = await insert_raw(raw_repository, payload, platform="qq_official")
        original = QQOfficialMessageParser().parse(payload, raw_event_id=raw.id)
        assert original is not None
        await message_repository.persist(original, parser_name="qq_official_message_parser", parser_version="1")
        loaded = await message_repository.get_by_raw_event_id(raw.id)
        assert loaded == original

        if filename == "012_merged_forward.json":
            forward = loaded.segments[0]
            assert isinstance(forward, ForwardSegment)
            assert forward.nodes == ()
            assert forward.resolution_status is ForwardResolutionStatus.UNRESOLVED
            assert forward.raw_data == payload["data"]
        if filename == "007_reply_text.json":
            reply = loaded.segments[0]
            assert isinstance(reply, ReplySegment)
            assert reply.referenced_message_id is None
            assert any(
                "ref_msg_idx=" in item
                for item in reply.raw_data["message_scene"]["ext"]
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_node_failure_rolls_back_normalized_tree_but_keeps_raw(tmp_path: Path) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    payload = {"post_type": "message", "message": [{"type": "text", "data": {"text": "safe"}}]}
    try:
        raw = await insert_raw(raw_repository, payload, platform="onebot11")
        parsed = OneBotMessageParser().parse(payload, source_raw_event_id=raw.id)
        assert parsed is not None
        invalid = replace(
            parsed,
            segments=(UnknownSegment(position=0, original_type="invalid_json", raw_data={1, 2}),),
        )

        with pytest.raises(Exception):
            await message_repository.persist(invalid, parser_name="parser", parser_version="1")

        assert await counts(database) == (1, 0, 0)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_normalized_failure_is_safe_and_next_raw_receipt_still_normalizes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    payload = json.loads(ONEBOT_FIXTURE.read_text(encoding="utf-8"))

    class FailingOnceRepository:
        def __init__(self) -> None:
            self.failed = False

        async def persist(self, message: Any, **metadata: Any) -> Any:
            if not self.failed:
                self.failed = True
                raise RuntimeError("simulated normalized failure")
            return await message_repository.persist(message, **metadata)

    service = NormalizedMessageIngestionService(
        repository=FailingOnceRepository(),  # type: ignore[arg-type]
        parser=OneBotMessageParser(),
    )
    caplog.set_level("ERROR", logger="app.services.normalized_message_ingestion")
    try:
        first = await insert_raw(raw_repository, copy.deepcopy(payload), platform="onebot11")
        second = await insert_raw(raw_repository, copy.deepcopy(payload), platform="onebot11")

        assert await service.ingest(first) is None
        assert await service.ingest(second) is not None
        assert await counts(database) == (2, 1, 1)
        assert "raw_event_id=" in caplog.text
        assert "error_type=RuntimeError" in caplog.text
        assert payload["message"][0]["data"]["text"] not in caplog.text
    finally:
        await database.dispose()
