from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.adapters.qq_official.gateway import QQGatewayDispatch
from app.llm import LLMRequest, LLMResponse
from app.parsers import QQOfficialMessageParser
from app.rendering import MessageRenderer, SummaryMessageFormatter
from app.services.command_dispatch import QQOfficialCommandDispatcher
from app.services.conversation_query import ConversationQueryService
from app.services.normalized_message_ingestion import (
    QQOfficialNormalizedMessageIngestionService,
)
from app.services.ping_command import PingCommandHandler, PingCommandStatus
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


def dispatch(
    *,
    timestamp: str,
    message_id: str,
    text: str,
    group_openid: str,
    author_id: str = "test-user",
) -> QQGatewayDispatch:
    return QQGatewayDispatch(
        sequence=1,
        event_type="GROUP_MESSAGE_CREATE",
        data={
            "author": {
                "id": author_id,
                "member_openid": author_id,
                "username": "测试成员",
            },
            "content": text,
            "group_id": group_openid,
            "group_openid": group_openid,
            "id": message_id,
            "message_type": 0,
            "timestamp": timestamp,
        },
    )


@pytest.mark.asyncio
async def test_ping_command_routes_group_transport_without_mutating_author(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'ping-command.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    message_repository = MessageRepository(database.session_factory)
    raw_ingestion = QQOfficialRawEventIngestionService(raw_repository)
    normalized_ingestion = QQOfficialNormalizedMessageIngestionService(
        repository=message_repository,
        parser=QQOfficialMessageParser(),
    )
    sender = FakeQQOfficialSender()
    dispatcher = QQOfficialCommandDispatcher(
        ping_handler=PingCommandHandler(sender=sender)
    )
    try:
        raw = await raw_ingestion.ingest(
            dispatch(
                timestamp="2026-08-11T12:00:00+08:00",
                message_id="test-msg-id",
                text="#ping",
                group_openid="test-group",
            )
        )
        assert raw is not None
        normalized = await normalized_ingestion.ingest(raw)
        assert normalized is not None

        status = await dispatcher.handle(normalized)

        assert status is PingCommandStatus.SUCCEEDED
        assert normalized.context.group_id == "test-group"
        assert normalized.author.user_id == "test-user"
        assert normalized.actor.user_id == "test-user"
        assert normalized.author.user_id != normalized.context.group_id
        assert sender.calls == [("test-group", "pong", "test-msg-id")]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_summary_command_routes_current_group_only_and_replies_to_trigger_message(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'summary-command.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    message_repository = MessageRepository(database.session_factory)
    raw_ingestion = QQOfficialRawEventIngestionService(raw_repository)
    normalized_ingestion = QQOfficialNormalizedMessageIngestionService(
        repository=message_repository,
        parser=QQOfficialMessageParser(),
    )
    provider = FakeLLMProvider()
    sender = FakeQQOfficialSender()
    summary_repository = SummaryRepository(database.session_factory)
    dispatcher = QQOfficialCommandDispatcher(
        ping_handler=PingCommandHandler(sender=sender),
        summary_handler=SummaryCommandHandler(
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
        ),
    )
    try:
        history_group_a = await raw_ingestion.ingest(
            dispatch(
                timestamp="2026-08-11T10:00:00+08:00",
                message_id="history-a",
                text="A组消息：明天按计划进行测试。",
                group_openid="group-a",
                author_id="member-a",
            )
        )
        history_group_b = await raw_ingestion.ingest(
            dispatch(
                timestamp="2026-08-11T10:30:00+08:00",
                message_id="history-b",
                text="B组消息：这条不应该出现在A组总结里。",
                group_openid="group-b",
                author_id="member-b",
            )
        )
        command_raw = await raw_ingestion.ingest(
            dispatch(
                timestamp="2026-08-11T11:00:00+08:00",
                message_id="command-a",
                text=" #总结 ",
                group_openid="group-a",
                author_id="member-a",
            )
        )
        assert history_group_a is not None
        assert history_group_b is not None
        assert command_raw is not None
        await normalized_ingestion.ingest(history_group_a)
        await normalized_ingestion.ingest(history_group_b)
        command_message = await normalized_ingestion.ingest(command_raw)
        assert command_message is not None
        assert command_message.timestamp == datetime.fromisoformat(
            "2026-08-11T11:00:00+08:00"
        ).astimezone(timezone.utc)

        status = await dispatcher.handle(command_message)

        assert status is SummaryCommandStatus.SUCCEEDED
        assert command_message.author.user_id == "member-a"
        assert command_message.context.group_id == "group-a"
        assert len(provider.requests) == 1
        prompt = provider.requests[0].user_prompt
        assert "A组消息：明天按计划进行测试。" in prompt
        assert "B组消息：这条不应该出现在A组总结里。" not in prompt
        assert "#总结" not in prompt
        history = await summary_repository.list_for_group(
            platform="qq_official",
            group_id="group-a",
        )
        assert len(history) == 1
        assert history[0].result.group_id == "group-a"
        assert sender.calls == [
            (
                "group-a",
                "【群聊总结】\n\n群内确认了测试安排。\n\n"
                "【主要话题】\n- 测试安排\n\n【关键内容】\n- 按计划执行",
                "command-a",
            )
        ]
    finally:
        await database.dispose()
