from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.domain.messages import TextSegment
from app.parsers import OneBotMessageParser, QQOfficialMessageParser
from app.rendering import MessageRenderer
from app.services.conversation_query import ConversationQueryService
from app.services.raw_event_ingestion import RawEventIngestionService
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


ONEBOT_FIXTURE = Path(__file__).parents[1] / "fixtures" / "onebot" / "real_group_text_sanitized.json"
QQ_SAMPLE = Path(__file__).parents[2] / "data" / "qq_official_samples" / "001_text.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["onebot11", "qq_official"])
async def test_raw_parser_persistence_query_renderer_chain(tmp_path: Path, platform: str) -> None:
    fixture_path = ONEBOT_FIXTURE if platform == "onebot11" else QQ_SAMPLE
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'{platform}.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    message_repository = MessageRepository(database.session_factory)
    try:
        fixture_message = (
            OneBotMessageParser().parse(payload, source_raw_event_id=1)
            if platform == "onebot11"
            else QQOfficialMessageParser().parse(payload, raw_event_id=1)
        )
        assert fixture_message is not None and fixture_message.timestamp is not None
        raw = await raw_repository.insert(
            RawEvent(
                platform=platform,
                received_at=fixture_message.timestamp,
                event_time=None,
                post_type="message",
                message_type="group",
                sub_type=None,
                self_id=None,
                user_id=None,
                group_id=None,
                message_id=None,
                raw_payload=payload,
                payload_hash=RawEventIngestionService.payload_hash(payload),
            )
        )
        parsed = (
            OneBotMessageParser().parse(payload, source_raw_event_id=raw.id)
            if platform == "onebot11"
            else QQOfficialMessageParser().parse(payload, raw_event_id=raw.id)
        )
        assert parsed is not None and parsed.timestamp is not None
        await message_repository.persist(
            parsed,
            parser_name=f"{platform}_parser",
            parser_version="1",
        )

        queried = await ConversationQueryService(message_repository).get_messages(
            platform=platform,
            group_id=parsed.context.group_id or "",
            start_time=parsed.timestamp - timedelta(seconds=1),
            end_time=parsed.timestamp + timedelta(seconds=1),
        )
        rendered = MessageRenderer().render_conversation(queried)

        assert queried == (parsed,)
        assert rendered
        expected_text = next(
            segment.text
            for segment in parsed.segments
            if isinstance(segment, TextSegment)
        )
        assert expected_text in rendered
        assert "raw_payload" not in rendered
    finally:
        await database.dispose()
