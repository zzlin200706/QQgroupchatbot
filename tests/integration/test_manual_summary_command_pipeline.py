from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.adapters.qq_official.gateway import QQGatewayDispatch
from app.llm import LLMRequest, LLMResponse
from app.parsers import QQOfficialMessageParser
from app.rendering import MessageRenderer, SummaryMessageFormatter
from app.services.conversation_query import ConversationQueryService
from app.services.normalized_message_ingestion import (
    QQOfficialNormalizedMessageIngestionService,
)
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.services.summary import SummaryService
from app.services.summary_command import SummaryCommandHandler, SummaryCommandStatus
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.raw_event_repository import RawEventRepository
from app.storage.summary_repository import SummaryRepository


class FakeLLMProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content=json.dumps(
                {
                    "summary": "群内确认了测试安排。",
                    "topics": ["测试安排"],
                    "key_points": ["按计划执行"],
                    "decisions": [],
                    "action_items": [],
                    "open_questions": [],
                },
                ensure_ascii=False,
            ),
            provider="fake-provider",
            model="fake-model",
            finish_reason="stop",
            usage=None,
        )


class FakeQQOfficialSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def send_group_message(
        self,
        group_id: str,
        message: str,
        *,
        msg_id: str | None = None,
    ) -> object:
        self.calls.append((group_id, message, msg_id))
        return object()


def dispatch(*, timestamp: str, message_id: str, text: str) -> QQGatewayDispatch:
    return QQGatewayDispatch(
        sequence=1,
        event_type="GROUP_MESSAGE_CREATE",
        data={
            "author": {
                "id": "member-1",
                "member_openid": "member-1",
                "username": "测试成员",
            },
            "content": text,
            "group_id": "group-1",
            "group_openid": "group-1",
            "id": message_id,
            "message_type": 0,
            "timestamp": timestamp,
        },
    )


@pytest.mark.asyncio
async def test_manual_command_closes_generation_persistence_send_loop(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'manual-command.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    message_repository = MessageRepository(database.session_factory)
    raw_ingestion = QQOfficialRawEventIngestionService(raw_repository)
    normalized_ingestion = QQOfficialNormalizedMessageIngestionService(
        repository=message_repository,
        parser=QQOfficialMessageParser(),
    )
    summary_repository = SummaryRepository(database.session_factory)
    provider = FakeLLMProvider()
    sender = FakeQQOfficialSender()
    handler = SummaryCommandHandler(
        summary_service=SummaryService(
            query_service=ConversationQueryService(message_repository),
            renderer=MessageRenderer(),
            provider=provider,
        ),
        summary_repository=summary_repository,
        formatter=SummaryMessageFormatter(),
        sender=sender,
        enabled=True,
        lookback_minutes=120,
        cooldown_seconds=60,
    )
    try:
        history_raw = await raw_ingestion.ingest(
            dispatch(
                timestamp="2026-08-10T16:00:00+08:00",
                message_id="history-1",
                text="明天按计划进行测试。",
            )
        )
        command_raw = await raw_ingestion.ingest(
            dispatch(
                timestamp="2026-08-10T17:00:00+08:00",
                message_id="command-1",
                text=" #总结 ",
            )
        )
        assert history_raw is not None and command_raw is not None
        history_message = await normalized_ingestion.ingest(history_raw)
        command_message = await normalized_ingestion.ingest(command_raw)
        assert history_message is not None and command_message is not None
        assert command_message.timestamp == datetime.fromisoformat(
            "2026-08-10T17:00:00+08:00"
        ).astimezone(timezone.utc)

        status = await handler.handle(command_message)

        assert status is SummaryCommandStatus.SUCCEEDED
        assert len(provider.requests) == 1
        prompt = provider.requests[0].user_prompt
        assert "明天按计划进行测试。" in prompt
        assert "#总结" not in prompt
        history = await summary_repository.list_for_group(
            platform="qq_official",
            group_id="group-1",
        )
        assert len(history) == 1
        assert history[0].result.summary == "群内确认了测试安排。"
        assert history[0].result.message_count == 1
        assert sender.calls == [
            (
                "group-1",
                "【群聊总结】\n\n群内确认了测试安排。\n\n"
                "【主要话题】\n- 测试安排\n\n【关键内容】\n- 按计划执行",
                "command-1",
            )
        ]
    finally:
        await database.dispose()
