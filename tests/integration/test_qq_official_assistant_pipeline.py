from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.qq_official import inbound_event_from_gateway_dispatch
from app.adapters.qq_official.gateway import QQGatewayDispatch
from app.llm import LLMRequest, LLMResponse
from app.parsers import QQOfficialMessageParser
from app.rendering import MessageRenderer
from app.services.command_dispatch import QQOfficialCommandDispatcher
from app.services.conversation_query import ConversationQueryService
from app.services.group_assistant import GroupAssistantService
from app.services.group_assistant_context import GroupAssistantContextBuilder
from app.services.group_assistant_handler import GroupAssistantHandler
from app.services.interaction_dispatch import QQOfficialInteractionDispatcher
from app.services.normalized_message_ingestion import (
    QQOfficialNormalizedMessageIngestionService,
)
from app.services.ping_command import PingCommandHandler
from app.services.qq_official_event_processor import QQOfficialEventProcessor
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.storage.assistant_interaction_repository import AssistantInteractionRepository
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.raw_event_repository import RawEventRepository


class FakeProvider:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content=self.answers[len(self.requests) - 1],
            provider="fake-provider",
            model="fake-model",
            finish_reason="stop",
            usage=None,
        )


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def send_group_message(
        self,
        group_id: str,
        content: str,
        *,
        msg_id: str | None = None,
    ) -> object:
        self.calls.append((group_id, content, msg_id))
        return SimpleNamespace(message_id=f"bot-response-{len(self.calls)}")


@dataclass
class Pipeline:
    database: Database
    processor: QQOfficialEventProcessor
    raw_repository: RawEventRepository
    message_repository: MessageRepository
    interaction_repository: AssistantInteractionRepository
    provider: FakeProvider
    sender: FakeSender


async def build_pipeline(tmp_path: Path, answers: list[str]) -> Pipeline:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'assistant-pipeline.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    message_repository = MessageRepository(database.session_factory)
    interaction_repository = AssistantInteractionRepository(database.session_factory)
    provider = FakeProvider(answers)
    sender = FakeSender()
    assistant = GroupAssistantHandler(
        service=GroupAssistantService(
            context_builder=GroupAssistantContextBuilder(
                query_service=ConversationQueryService(message_repository),
                interaction_repository=interaction_repository,
                renderer=MessageRenderer(),
                qa_lookback_minutes=120,
                qa_max_messages=150,
                chat_lookback_minutes=30,
                chat_max_messages=80,
                chat_max_assistant_turns=20,
                max_input_chars=40000,
            ),
            provider=provider,
            max_output_tokens=1200,
            max_output_chars=4500,
        ),
        repository=interaction_repository,
        sender=sender,
        enabled=True,
        cooldown_seconds=0,
    )
    commands = QQOfficialCommandDispatcher(
        ping_handler=PingCommandHandler(sender=sender)
    )
    processor = QQOfficialEventProcessor(
        raw_ingestion_service=QQOfficialRawEventIngestionService(raw_repository),
        normalized_ingestion_service=QQOfficialNormalizedMessageIngestionService(
            repository=message_repository,
            parser=QQOfficialMessageParser(),
        ),
        interaction_dispatcher=QQOfficialInteractionDispatcher(
            command_dispatcher=commands,
            assistant_handler=assistant,
        ),
    )
    return Pipeline(
        database=database,
        processor=processor,
        raw_repository=raw_repository,
        message_repository=message_repository,
        interaction_repository=interaction_repository,
        provider=provider,
        sender=sender,
    )


def dispatch(
    *,
    group_id: str,
    message_id: str,
    content: str,
    timestamp: str,
    author_id: str = "user-a",
    event_type: str = "GROUP_MESSAGE_CREATE",
) -> QQGatewayDispatch:
    data = {
        "author": {
            "id": author_id,
            "member_openid": author_id,
            "username": author_id,
        },
        "content": content,
        "group_id": group_id,
        "group_openid": group_id,
        "id": message_id,
        "message_type": 0,
        "timestamp": timestamp,
    }
    return QQGatewayDispatch(
        sequence=1,
        event_type=event_type,
        data=data,
        raw_payload={"op": 0, "s": 1, "t": event_type, "d": data},
    )


async def process(pipeline: Pipeline, event: QQGatewayDispatch) -> None:
    await pipeline.processor.process(inbound_event_from_gateway_dispatch(event))
    await pipeline.processor.drain()


@pytest.mark.asyncio
async def test_grounded_qa_is_raw_first_group_scoped_and_persisted_after_send(
    tmp_path: Path,
) -> None:
    pipeline = await build_pipeline(tmp_path, ["明天下午两点。"])
    try:
        # Equal timestamps exercise the raw receipt ordering boundary.
        timestamp = "2026-08-11T12:00:00+08:00"
        await process(
            pipeline,
            dispatch(
                group_id="group-a",
                message_id="history-a",
                content="明天下午两点开会，地点是 305。",
                timestamp=timestamp,
            ),
        )
        await process(
            pipeline,
            dispatch(
                group_id="group-b",
                message_id="history-b",
                content="B组秘密：下午五点。",
                timestamp=timestamp,
                author_id="user-b",
            ),
        )
        await process(
            pipeline,
            dispatch(
                group_id="group-a",
                message_id="qa-trigger",
                content="#问 明天几点开会？",
                timestamp=timestamp,
            ),
        )

        assert len(await pipeline.raw_repository.list_recent()) == 3
        assert len(pipeline.provider.requests) == 1
        prompt = pipeline.provider.requests[0].user_prompt
        assert "明天下午两点开会，地点是 305。" in prompt
        assert "B组秘密" not in prompt
        assert "#问 明天几点开会？" not in prompt
        assert pipeline.sender.calls == [
            ("group-a", "明天下午两点。", "qa-trigger")
        ]
        history = await pipeline.interaction_repository.list_recent_for_group(
            platform="qq_official",
            group_id="group-a",
            start_time=datetime.fromisoformat("2026-08-11T11:00:00+08:00"),
            before_time=datetime.fromisoformat("2026-08-11T13:00:00+08:00"),
            limit=20,
        )
        assert len(history) == 1
        assert history[0].interaction.response_message_id == "bot-response-1"
        assert history[0].interaction.requester.user_id == "user-a"
        assert history[0].interaction.group_id == "group-a"
    finally:
        await pipeline.processor.aclose()
        await pipeline.database.dispose()


@pytest.mark.asyncio
async def test_group_at_chat_uses_general_context_and_successful_assistant_history(
    tmp_path: Path,
) -> None:
    pipeline = await build_pipeline(
        tmp_path,
        [
            "B组机器人回答，不得进入A组。",
            "GIL 是解释器锁。",
            "多进程使用独立解释器进程。",
        ],
    )
    try:
        await process(
            pipeline,
            dispatch(
                group_id="group-b",
                message_id="chat-b",
                content=" B组问题",
                timestamp="2026-08-11T11:59:00+08:00",
                author_id="user-b",
                event_type="GROUP_AT_MESSAGE_CREATE",
            ),
        )
        await process(
            pipeline,
            dispatch(
                group_id="group-a",
                message_id="context-a",
                content="我们项目现在主要使用 SQLite。",
                timestamp="2026-08-11T12:00:00+08:00",
            ),
        )
        await process(
            pipeline,
            dispatch(
                group_id="group-a",
                message_id="chat-1",
                content=" Python GIL 是什么？",
                timestamp="2026-08-11T12:01:00+08:00",
                event_type="GROUP_AT_MESSAGE_CREATE",
            ),
        )
        await process(
            pipeline,
            dispatch(
                group_id="group-a",
                message_id="chat-2",
                content=" 那多进程为什么可以绕开？",
                timestamp="2026-08-11T12:02:00+08:00",
                event_type="GROUP_AT_MESSAGE_CREATE",
            ),
        )

        assert len(pipeline.provider.requests) == 3
        first_prompt = pipeline.provider.requests[1].user_prompt
        second_prompt = pipeline.provider.requests[2].user_prompt
        assert "我们项目现在主要使用 SQLite。" in first_prompt
        assert "GIL 是解释器锁。" in second_prompt
        assert "机器人生成内容，不是群聊事实" in second_prompt
        assert "B组机器人回答" not in first_prompt
        assert "B组机器人回答" not in second_prompt
        assert pipeline.provider.requests[1].json_output is False
        assert [call[2] for call in pipeline.sender.calls] == [
            "chat-b",
            "chat-1",
            "chat-2",
        ]
    finally:
        await pipeline.processor.aclose()
        await pipeline.database.dispose()


@pytest.mark.asyncio
async def test_ordinary_messages_persist_without_assistant_or_llm(tmp_path: Path) -> None:
    pipeline = await build_pipeline(tmp_path, [])
    ordinary = [
        "你好",
        "测试bot",
        "ping",
        "今天吃什么",
    ]
    try:
        for index, content in enumerate(ordinary):
            await process(
                pipeline,
                dispatch(
                    group_id="group-a",
                    message_id=f"ordinary-{index}",
                    content=content,
                    timestamp=f"2026-08-11T12:0{index}:00+08:00",
                ),
            )

        assert len(await pipeline.raw_repository.list_recent()) == len(ordinary)
        assert pipeline.provider.requests == []
        assert pipeline.sender.calls == []
    finally:
        await pipeline.processor.aclose()
        await pipeline.database.dispose()
