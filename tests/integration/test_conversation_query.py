from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

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
from app.services.conversation_query import ConversationQueryService
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


UTC = timezone.utc
TEN = datetime(2026, 8, 10, 10, tzinfo=UTC)


async def storage(
    tmp_path: Path,
) -> tuple[Database, RawEventRepository, MessageRepository, ConversationQueryService]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'conversation.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    message_repository = MessageRepository(database.session_factory)
    return (
        database,
        raw_repository,
        message_repository,
        ConversationQueryService(message_repository),
    )


async def raw_receipt(repository: RawEventRepository, marker: str) -> RawEvent:
    payload: dict[str, Any] = {"marker": marker}
    return await repository.insert(
        RawEvent(
            platform="qq_official",
            received_at=TEN,
            event_time=None,
            post_type=None,
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            self_id=None,
            user_id=None,
            group_id=None,
            message_id=None,
            raw_payload=payload,
            payload_hash=QQOfficialRawEventIngestionService.payload_hash(payload),
        )
    )


def normalized(
    raw_event_id: int,
    text: str,
    *,
    timestamp: datetime | None,
    platform: str = "qq_official",
    group_id: str = "group-a",
    platform_message_id: str | None = None,
) -> InternalMessage:
    author = IdentityRef(
        platform=platform,
        user_id="user-1",
        display_name="Alice",
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )
    return InternalMessage(
        platform=platform,
        source_raw_event_id=raw_event_id,
        platform_message_id=platform_message_id or f"message-{raw_event_id}",
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id=group_id,
        ),
        actor=author,
        author=author,
        timestamp=timestamp,
        segments=(TextSegment(position=0, raw_data={"text": text}, text=text),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=raw_event_id,
        ),
    )


def texts(messages) -> list[str | None]:
    return [message.segments[0].text for message in messages]


@pytest.mark.asyncio
async def test_half_open_range_excludes_null_and_limit_keeps_earliest(
    tmp_path: Path,
) -> None:
    database, raws, messages, query = await storage(tmp_path)
    try:
        for marker, timestamp in (
            ("A", TEN),
            ("B", TEN + timedelta(minutes=30)),
            ("C", TEN + timedelta(hours=1)),
            ("NULL", None),
        ):
            raw = await raw_receipt(raws, marker)
            await messages.persist(
                normalized(raw.id, marker, timestamp=timestamp),
                parser_name="parser",
                parser_version="1",
            )

        result = await query.get_messages(
            platform="qq_official",
            group_id="group-a",
            start_time=TEN,
            end_time=TEN + timedelta(hours=1),
        )
        limited = await query.get_messages(
            platform="qq_official",
            group_id="group-a",
            start_time=TEN,
            end_time=TEN + timedelta(hours=2),
            limit=2,
        )

        assert texts(result) == ["A", "B"]
        assert texts(limited) == ["A", "B"]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_platform_and_group_isolation_remain_explicit(tmp_path: Path) -> None:
    database, raws, messages, query = await storage(tmp_path)
    try:
        cases = (
            ("first", "qq_official", "same"),
            ("second", "qq_official", "same"),
            ("other-group", "qq_official", "other"),
            ("other-platform", "other_platform", "same"),
        )
        for text, platform, group_id in cases:
            raw = await raw_receipt(raws, text)
            await messages.persist(
                normalized(
                    raw.id,
                    text,
                    timestamp=TEN,
                    platform=platform,
                    group_id=group_id,
                ),
                parser_name="parser",
                parser_version="1",
            )

        result = await query.get_messages(
            platform="qq_official",
            group_id="same",
            start_time=TEN,
            end_time=TEN + timedelta(minutes=1),
        )

        assert texts(result) == ["first", "second"]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_parser_replay_selection_and_explicit_parser_scope(tmp_path: Path) -> None:
    database, raws, messages, query = await storage(tmp_path)
    try:
        raw = await raw_receipt(raws, "replay")
        base = normalized(raw.id, "parser-a-v1", timestamp=TEN)
        await messages.persist(base, parser_name="parser-a", parser_version="1")
        await messages.persist(
            replace(
                base,
                segments=(TextSegment(position=0, raw_data=None, text="parser-b-v1"),),
            ),
            parser_name="parser-b",
            parser_version="1",
        )
        await messages.persist(
            replace(
                base,
                segments=(TextSegment(position=0, raw_data=None, text="parser-a-v10"),),
            ),
            parser_name="parser-a",
            parser_version="10",
        )

        common = dict(
            platform="qq_official",
            group_id="group-a",
            start_time=TEN,
            end_time=TEN + timedelta(minutes=1),
        )
        latest = await query.get_messages(**common)
        parser_a_latest = await query.get_messages(**common, parser_name="parser-a")
        parser_a_v1 = await query.get_messages(
            **common,
            parser_name="parser-a",
            parser_version="1",
        )
        parser_b = await query.get_messages(**common, parser_name="parser-b")

        assert texts(latest) == ["parser-a-v10"]
        assert texts(parser_a_latest) == ["parser-a-v10"]
        assert texts(parser_a_v1) == ["parser-a-v1"]
        assert texts(parser_b) == ["parser-b-v1"]
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"platform": "", "group_id": "group-a", "limit": 1},
        {"platform": "qq_official", "group_id": "", "limit": 1},
        {"platform": "qq_official", "group_id": "group-a", "limit": 0},
    ],
)
async def test_query_rejects_invalid_arguments(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    database, _, _, query = await storage(tmp_path)
    try:
        with pytest.raises(ValueError):
            await query.get_messages(
                start_time=TEN,
                end_time=TEN + timedelta(minutes=1),
                **kwargs,  # type: ignore[arg-type]
            )
    finally:
        await database.dispose()
