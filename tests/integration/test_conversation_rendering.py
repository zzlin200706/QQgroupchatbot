from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.parsers import QQOfficialMessageParser
from app.rendering import MessageRenderer
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


QQ_SAMPLE = Path(__file__).parents[2] / "data" / "qq_official_samples" / "001_text.json"


@pytest.mark.asyncio
async def test_qq_official_sample_renders_consistently_after_db_round_trip(
    tmp_path: Path,
) -> None:
    payload = json.loads(QQ_SAMPLE.read_text(encoding="utf-8"))
    parsed = QQOfficialMessageParser().parse(payload, raw_event_id=1)
    assert parsed is not None and parsed.timestamp is not None

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'rendering.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    message_repository = MessageRepository(database.session_factory)
    try:
        raw = await raw_repository.insert(
            RawEvent(
                platform="qq_official",
                received_at=parsed.timestamp,
                event_time=parsed.timestamp,
                post_type=None,
                message_type="0",
                sub_type="GROUP_MESSAGE_CREATE",
                self_id=None,
                user_id=parsed.actor.user_id,
                group_id=parsed.context.group_id,
                message_id=parsed.platform_message_id,
                raw_payload=payload,
                payload_hash=QQOfficialRawEventIngestionService.payload_hash(payload),
            )
        )
        await message_repository.persist(
            QQOfficialMessageParser().parse(payload, raw_event_id=raw.id),
            parser_name="qq_official_message_parser",
            parser_version="1",
        )
        loaded = await message_repository.get_by_raw_event_id(
            raw.id,
            parser_name="qq_official_message_parser",
            parser_version="1",
        )
        assert loaded is not None

        rendered = MessageRenderer().render_conversation((loaded,))

        assert "phase-d-test-001" in rendered
        assert "A8CA550978D826B0784F510188F5BE35" not in rendered
        assert "ROBOT1.0_" not in rendered
    finally:
        await database.dispose()
