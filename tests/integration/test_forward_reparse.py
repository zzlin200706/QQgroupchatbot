from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.parsers import QQOfficialMessageParser
from app.domain.messages import TextSegment
from app.rendering import MessageRenderer
from app.services.conversation_query import ConversationQueryService
from app.services.forward_reparse import reparse_forwards
from app.services.normalized_message_ingestion import (
    QQ_OFFICIAL_PARSER_NAME, QQ_OFFICIAL_PARSER_VERSION,
)
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.raw_event_repository import RawEventRepository
from app.storage.models import (
    AssistantInteractionRecord, CommandTriggerClaimRecord,
    InteractionOutboxRecord, MessageRecord, RawEvent, SummaryRecord,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("single_receipt", [False, True])
async def test_preview_apply_and_repeat_preserve_facts_without_replaying(
    tmp_path, single_receipt,
):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'reparse.db'}")
    await db.initialize()
    raw_repo = RawEventRepository(db.session_factory)
    messages = MessageRepository(db.session_factory)
    parser = QQOfficialMessageParser()
    timestamp = datetime.now(timezone.utc)
    receipts = []
    try:
        for index, kind in enumerate((102, 102, 0)):
            payload = {"t": "GROUP_MESSAGE_CREATE", "d": {
                "id": f"m-{index}", "group_openid": "g", "message_type": kind,
                "timestamp": timestamp.isoformat(),
                "author": {"id": "forwarder", "username": "转发者"},
                "content": f"转发正文{index}：周五发布。",
            }}
            raw = await raw_repo.insert(RawEvent(
                platform="qq_official", received_at=timestamp,
                raw_payload=payload, payload_hash=str(index),
            ))
            receipts.append(raw)
            message = parser.parse(payload, raw_event_id=raw.id)
            if kind == 102:
                message = replace(message, segments=(replace(message.segments[0], content=()),))
            await messages.persist(
                message, parser_name=QQ_OFFICIAL_PARSER_NAME, parser_version="1",
            )
        scope = receipts[0].id if single_receipt else None
        expected = 1 if single_receipt else 2
        preview = await reparse_forwards(
            db.session_factory, parser=parser, raw_event_id=scope, batch_size=1,
        )
        assert preview.candidates == expected
        assert preview.persisted == preview.failed == 0
        async with db.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(MessageRecord)) == 3

        applied = await reparse_forwards(
            db.session_factory, parser=parser, apply=True,
            raw_event_id=scope, batch_size=1,
        )
        assert applied.persisted == expected and applied.failed == 0
        again = await reparse_forwards(
            db.session_factory, parser=parser, apply=True,
            raw_event_id=scope, batch_size=1,
        )
        assert again.candidates == again.persisted == again.failed == 0
        for raw in receipts[:expected]:
            legacy = await messages.get_by_raw_event_id(
                raw.id, parser_name=QQ_OFFICIAL_PARSER_NAME, parser_version="1",
            )
            assert legacy.segments[0].content == ()
            current = await messages.get_by_raw_event_id(
                raw.id, parser_name=QQ_OFFICIAL_PARSER_NAME,
                parser_version=QQ_OFFICIAL_PARSER_VERSION,
            )
            assert current.segments[0].content[0].text == raw.raw_payload['d']['content']
            assert (await raw_repo.get_by_id(raw.id)).raw_payload == raw.raw_payload
        # The normal context query selects the new version, not both versions.
        window = await ConversationQueryService(messages).get_messages(
            platform="qq_official", group_id="g",
            start_time=timestamp - timedelta(seconds=1),
            end_time=timestamp + timedelta(seconds=1),
        )
        assert len(window) == 3
        rendered = MessageRenderer().render_conversation(window)
        assert rendered.count("转发正文0") == 1
        async with db.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(RawEvent)) == 3
            assert await session.scalar(select(func.count()).select_from(MessageRecord)) == 3 + expected
            for table in (InteractionOutboxRecord, AssistantInteractionRecord,
                          CommandTriggerClaimRecord, SummaryRecord):
                assert await session.scalar(select(func.count()).select_from(table)) == 0
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_bad_receipt_is_reported_and_not_deleted(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'bad-raw.db'}")
    await db.initialize()
    try:
        repo = RawEventRepository(db.session_factory)
        raw = await repo.insert(RawEvent(
            platform="qq_official", received_at=datetime.now(timezone.utc),
            raw_payload=["malformed envelope"], payload_hash="bad",
        ))
        result = await reparse_forwards(
            db.session_factory, parser=QQOfficialMessageParser(), apply=True,
        )
        assert result.failed == 1
        assert result.persisted == 0
        assert (await repo.get_by_id(raw.id)).raw_payload == ["malformed envelope"]
    finally:
        await db.dispose()


async def test_v2_shallow_quoted_forward_is_rebuilt_as_v3_without_losing_raw_or_old_tree(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'nested-v3.db'}")
    await db.initialize()
    try:
        raw_repo = RawEventRepository(db.session_factory)
        messages = MessageRepository(db.session_factory)
        now = datetime.now(timezone.utc)
        payload = {"t": "GROUP_MESSAGE_CREATE", "d": {
            "id": "old-nested-reply", "group_openid": "g", "message_type": 103,
            "timestamp": now.isoformat(), "content": "外层问题", "author": {"id": "forwarder"},
            "msg_elements": [{"message_type": 103, "content": "中层说明", "msg_elements": [
                {"message_type": 102, "msg_elements": [{"content": "旧版遗漏的内层事实"}]},
            ]}],
        }}
        raw = await raw_repo.insert(RawEvent(platform="qq_official", received_at=now,
                                             raw_payload=payload, payload_hash="unchanged-nested-hash"))
        parsed = QQOfficialMessageParser().parse(payload, raw_event_id=raw.id)
        outer = parsed.segments[0]
        shallow = replace(outer, resolved_message=replace(outer.resolved_message,
            segments=(TextSegment(position=0, raw_data="中层说明", text="中层说明"),)))
        old = replace(parsed, segments=(shallow, *parsed.segments[1:]))
        await messages.persist(old, parser_name=QQ_OFFICIAL_PARSER_NAME, parser_version="2")
        assert "旧版遗漏的内层事实" not in MessageRenderer().render_message(
            await messages.get_by_raw_event_id(raw.id))
        preview = await reparse_forwards(db.session_factory, parser=QQOfficialMessageParser())
        assert preview.candidates == 1 and preview.persisted == 0
        result = await reparse_forwards(db.session_factory, parser=QQOfficialMessageParser(), apply=True)
        assert result.persisted == 1
        rebuilt = await messages.get_by_raw_event_id(raw.id)
        assert "旧版遗漏的内层事实" in MessageRenderer().render_message(rebuilt)
        async with db.session_factory() as session:
            versions = list((await session.scalars(select(MessageRecord.parser_version))).all())
            assert sorted(versions) == ["2", "3"]
            assert await session.scalar(select(func.count()).select_from(InteractionOutboxRecord)) == 0
        saved = await raw_repo.get_by_id(raw.id)
        assert saved.raw_payload == payload and saved.payload_hash == "unchanged-nested-hash"
        again = await reparse_forwards(db.session_factory, parser=QQOfficialMessageParser(), apply=True)
        assert again.persisted == 0
    finally:
        await db.dispose()
