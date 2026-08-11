from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from app.adapters.qq_official.gateway import QQGatewayDispatch
from app.domain.messages import (
    ForwardNode,
    ForwardResolutionStatus,
    ForwardSegment,
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
from app.parsers import QQOfficialMessageParser
from app.services.normalized_message_ingestion import (
    QQOfficialNormalizedMessageIngestionService,
)
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


QQ_SAMPLES = Path(__file__).parents[2] / "data" / "qq_official_samples"


async def create_storage(tmp_path: Path) -> tuple[Database, RawEventRepository, MessageRepository]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'normalized.db'}")
    await database.initialize()
    return (
        database,
        RawEventRepository(database.session_factory),
        MessageRepository(database.session_factory),
    )


def sample(filename: str) -> dict:
    return json.loads((QQ_SAMPLES / filename).read_text(encoding="utf-8"))


def dispatch_from_sample(filename: str, *, sequence: int = 1) -> QQGatewayDispatch:
    payload = sample(filename)
    return QQGatewayDispatch(
        sequence=sequence,
        event_type=payload["gateway"]["t"],
        data=payload["data"],
    )


def received_at(payload: dict) -> datetime:
    parsed = QQOfficialMessageParser().parse(payload, raw_event_id=1)
    assert parsed is not None
    return parsed.timestamp or datetime(2026, 8, 10, tzinfo=timezone.utc)


def synthetic_identity(
    user_id: str | None,
    *,
    display_name: str | None,
    source: IdentitySource,
    availability: IdentityAvailability,
) -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id=user_id,
        display_name=display_name,
        card=None,
        source=source,
        availability=availability,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    [
        "001_text.json",
        "007_reply_text.json",
        "008_reply_image.json",
        "015_nested_forward_level2.json",
    ],
)
async def test_qq_official_samples_round_trip_strictly(
    tmp_path: Path,
    filename: str,
) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    payload = sample(filename)
    try:
        raw = await raw_repository.insert(
            RawEvent(
                platform="qq_official",
                received_at=received_at(payload),
                event_time=None,
                post_type=None,
                message_type=None,
                sub_type=None,
                self_id=None,
                user_id=None,
                group_id=None,
                message_id=None,
                raw_payload=payload,
                payload_hash=QQOfficialRawEventIngestionService.payload_hash(payload),
            )
        )
        original = QQOfficialMessageParser().parse(payload, raw_event_id=raw.id)
        assert original is not None

        await message_repository.persist(
            original,
            parser_name="qq_official_message_parser",
            parser_version="1",
        )
        loaded = await message_repository.get_by_raw_event_id(raw.id)

        assert loaded == original
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_forward_tree_survives_repository_round_trip(tmp_path: Path) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    raw_payload = sample("001_text.json")
    raw = await raw_repository.insert(
        RawEvent(
            platform="qq_official",
            received_at=received_at(raw_payload),
            event_time=None,
            post_type=None,
            message_type=None,
            sub_type=None,
            self_id=None,
            user_id=None,
            group_id=None,
            message_id=None,
            raw_payload=raw_payload,
            payload_hash=QQOfficialRawEventIngestionService.payload_hash(raw_payload),
        )
    )
    assert raw.id is not None
    actor = synthetic_identity(
        "event-sender",
        display_name="Event Sender",
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )
    nested_unknown = synthetic_identity(
        None,
        display_name=None,
        source=IdentitySource.UNKNOWN,
        availability=IdentityAvailability.UNAVAILABLE,
    )
    message = InternalMessage(
        platform="qq_official",
        source_raw_event_id=raw.id,
        platform_message_id="synthetic-forward-message",
        context=MessageContext(
            message_type="102",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="group-openid-1",
        ),
        actor=actor,
        author=actor,
        timestamp=datetime(2026, 8, 10, 10, tzinfo=timezone.utc),
        segments=(
            ForwardSegment(
                position=0,
                raw_data={"source": "synthetic-forward-tree"},
                reference_id=None,
                resolved=True,
                resolution_status=ForwardResolutionStatus.EMBEDDED,
                content=(),
                nodes=(
                    ForwardNode(
                        sender=synthetic_identity(
                            "node-1",
                            display_name="Bob",
                            source=IdentitySource.FORWARD_NODE,
                            availability=IdentityAvailability.KNOWN,
                        ),
                        timestamp=datetime(2026, 8, 10, 9, tzinfo=timezone.utc),
                        content=(TextSegment(position=0, raw_data=None, text="hello"),),
                        provenance=MessageProvenance(
                            source_type=ProvenanceSource.FORWARD_NODE,
                            raw_event_id=raw.id,
                            forward_depth=1,
                        ),
                        raw_data={"node": 1},
                    ),
                    ForwardNode(
                        sender=synthetic_identity(
                            None,
                            display_name=None,
                            source=IdentitySource.FORWARD_NODE,
                            availability=IdentityAvailability.UNAVAILABLE,
                        ),
                        timestamp=datetime(2026, 8, 10, 9, 1, tzinfo=timezone.utc),
                        content=(
                            ForwardSegment(
                                position=0,
                                raw_data={"source": "nested-forward"},
                                reference_id=None,
                                resolved=True,
                                resolution_status=ForwardResolutionStatus.EMBEDDED,
                                content=(),
                                nodes=(
                                    ForwardNode(
                                        sender=nested_unknown,
                                        timestamp=datetime(
                                            2026,
                                            8,
                                            10,
                                            9,
                                            2,
                                            tzinfo=timezone.utc,
                                        ),
                                        content=(
                                            ImageSegment(
                                                position=0,
                                                raw_data={"url": "https://example.invalid/image"},
                                                file="nested-image.jpg",
                                                url="https://example.invalid/image",
                                                summary=None,
                                                sub_type=None,
                                                file_size=123,
                                            ),
                                        ),
                                        provenance=MessageProvenance(
                                            source_type=ProvenanceSource.NESTED_FORWARD_NODE,
                                            raw_event_id=raw.id,
                                            forward_depth=2,
                                        ),
                                        raw_data={"node": 2},
                                    ),
                                ),
                            ),
                        ),
                        provenance=MessageProvenance(
                            source_type=ProvenanceSource.FORWARD_NODE,
                            raw_event_id=raw.id,
                            forward_depth=1,
                        ),
                        raw_data={"node": "parent"},
                    ),
                ),
            ),
        ),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=raw.id,
        ),
    )
    try:
        stored = await message_repository.persist(
            message,
            parser_name="qq_official_message_parser",
            parser_version="1",
        )
        loaded = await message_repository.get_by_raw_event_id(raw.id)

        assert stored == message
        assert loaded == message
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_same_raw_receipt_parser_version_is_idempotent(tmp_path: Path) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    payload = sample("001_text.json")
    try:
        raw = await raw_repository.insert(
            RawEvent(
                platform="qq_official",
                received_at=received_at(payload),
                event_time=None,
                post_type=None,
                message_type=None,
                sub_type=None,
                self_id=None,
                user_id=None,
                group_id=None,
                message_id=None,
                raw_payload=payload,
                payload_hash=QQOfficialRawEventIngestionService.payload_hash(payload),
            )
        )
        parsed = QQOfficialMessageParser().parse(payload, raw_event_id=raw.id)
        assert parsed is not None

        first = await message_repository.persist(
            parsed,
            parser_name="qq_official_message_parser",
            parser_version="1",
        )
        second = await message_repository.persist(
            parsed,
            parser_name="qq_official_message_parser",
            parser_version="1",
        )

        assert first == second
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_live_qq_gateway_pipeline_commits_raw_before_normalized_message(
    tmp_path: Path,
) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    raw_ingestion = QQOfficialRawEventIngestionService(raw_repository)
    normalized_ingestion = QQOfficialNormalizedMessageIngestionService(
        repository=message_repository,
        parser=QQOfficialMessageParser(),
    )
    try:
        stored_raw = await raw_ingestion.ingest(dispatch_from_sample("001_text.json"))
        assert stored_raw is not None
        assert await raw_repository.get_by_id(stored_raw.id) is not None

        stored_message = await normalized_ingestion.ingest(stored_raw)

        assert stored_message is not None
        assert stored_message.source_raw_event_id == stored_raw.id
        assert (
            await message_repository.get_by_raw_event_id(
                stored_raw.id,
                parser_name="qq_official_message_parser",
                parser_version="1",
            )
            == stored_message
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_non_group_dispatch_is_stored_but_not_normalized(tmp_path: Path) -> None:
    database, raw_repository, message_repository = await create_storage(tmp_path)
    raw_ingestion = QQOfficialRawEventIngestionService(raw_repository)
    normalized_ingestion = QQOfficialNormalizedMessageIngestionService(
        repository=message_repository,
        parser=QQOfficialMessageParser(),
    )
    try:
        raw = await raw_ingestion.ingest(
            QQGatewayDispatch(
                sequence=9,
                event_type="READY",
                data={"session_id": "session-1"},
            )
        )
        assert raw is not None
        assert await normalized_ingestion.ingest(raw) is None
    finally:
        await database.dispose()
