from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.adapters.qq_official.gateway import QQGatewayDispatch
from app.adapters.qq_official.auth import QQAccessToken
from app.adapters.qq_official.message_api import QQOfficialGroupMessageSender
from app.domain.images import ImageDescription, ResolvedImage
from app.domain.messages import ImageSegment
from app.llm import LLMRequest, LLMResponse
from app.parsers import QQOfficialMessageParser
from app.rendering import MessageRenderer, SummaryMessageFormatter
from app.services.conversation_query import ConversationQueryService
from app.services.normalized_message_ingestion import (
    QQOfficialNormalizedMessageIngestionService,
)
from app.services.image_enrichment import ImageEnrichmentService
from app.services.interaction_outbox_worker import InteractionOutboxWorker
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.services.summary import SummaryService
from app.services.command_status import CommandStatus
from app.services.summary_command import SummaryCommandHandler
from app.storage.database import Database
from app.storage.command_trigger_claim_repository import CommandTriggerClaimRepository
from app.storage.interaction_outbox_repository import InteractionOutboxRepository
from app.storage.message_repository import MessageRepository
from app.storage.models import RawEvent
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_status", "body"),
    [
        (200, {"err_code": 40034005, "message": "expired", "id": "not-delivered"}),
        (202, {"err_code": 0, "id": "pending"}),
    ],
)
async def test_unconfirmed_qq_send_keeps_summary_and_fails_outbox_without_retry(
    tmp_path: Path, http_status: int, body: dict,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'unconfirmed-send.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    raw_ingestion = QQOfficialRawEventIngestionService(raw_repository)
    message_repository = MessageRepository(database.session_factory)
    normalized_ingestion = QQOfficialNormalizedMessageIngestionService(
        repository=message_repository, parser=QQOfficialMessageParser(),
    )
    summary_repository = SummaryRepository(database.session_factory)
    outbox_repository = InteractionOutboxRepository(database.session_factory)
    provider = FakeLLMProvider()
    requests = []
    now = datetime.now(timezone.utc)

    class Auth:
        async def fetch_access_token(self) -> QQAccessToken:
            return QQAccessToken(access_token="fake-token", expires_in=7200)

        def invalidate(self) -> None:
            pytest.fail("A business error must not cause blind auth refresh/retry")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(http_status, json=body)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            handler = SummaryCommandHandler(
                summary_service=SummaryService(
                    query_service=ConversationQueryService(message_repository),
                    renderer=MessageRenderer(), provider=provider,
                ),
                summary_repository=summary_repository,
                formatter=SummaryMessageFormatter(),
                sender=QQOfficialGroupMessageSender(auth_client=Auth(), http_client=client),
                claim_repository=CommandTriggerClaimRepository(database.session_factory),
                enabled=True, cooldown_seconds=0,
            )
            for message_id, text, timestamp in (
                ("history", "明天按计划进行测试。", now - timedelta(minutes=1)),
                ("summary-command", "#总结", now),
            ):
                raw = await raw_ingestion.ingest(dispatch(
                    timestamp=timestamp.isoformat(), message_id=message_id, text=text,
                ))
                assert raw is not None
                message = await normalized_ingestion.ingest(raw)
                assert message is not None
            row, created = await outbox_repository.create_pending(
                platform="qq_official", group_id="group-1",
                trigger_message_id="summary-command", interaction_name="summary",
                source_raw_event_id=raw.id,
            )
            assert created
            worker = InteractionOutboxWorker(
                session_factory=database.session_factory,
                outbox_repository=outbox_repository,
                raw_event_repository=raw_repository,
                interaction_repository=None, on_handle=handler.handle,
                poll_interval_seconds=0.01,
            )
            await worker.start()
            try:
                async def wait_for_terminal():
                    while True:
                        current = await outbox_repository.get_by_id(row.id)
                        assert current is not None
                        if current.status in {"failed", "completed"}:
                            return current
                        await asyncio.sleep(0.01)

                failed = await asyncio.wait_for(wait_for_terminal(), timeout=3)
            finally:
                await worker.stop()
            assert failed.status == "failed"
            assert failed.attempt_count == 1
            assert failed.response_message_id is None
            assert "send_failed" in failed.last_error
            assert "automatic retry disabled" in failed.last_error

            # Restart reconciliation leaves this terminal row untouched;
            # the durable command claim also rejects direct redelivery.
            assert await worker.recover() == 0
            assert await outbox_repository.claim_next_pending() is None
            assert await handler.handle(message) is CommandStatus.DUPLICATE
            assert len(requests) == 1
            assert len(provider.requests) == 1
            summaries = await summary_repository.list_for_group(
                platform="qq_official", group_id="group-1",
            )
            assert len(summaries) == 1
            assert summaries[0].result.summary == "群内确认了测试安排。"
            async with database.session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(RawEvent)) == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["oversized", "invalid_chunk", "invalid_synthesis"])
async def test_incomplete_long_summary_never_persists_or_sends_and_keeps_raw(
    tmp_path: Path, failure: str,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'incomplete-summary.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    message_repository = MessageRepository(database.session_factory)
    raw_ingestion = QQOfficialRawEventIngestionService(raw_repository)
    normalized_ingestion = QQOfficialNormalizedMessageIngestionService(
        repository=message_repository, parser=QQOfficialMessageParser(),
    )
    summary_repository = SummaryRepository(database.session_factory)

    class FailingProvider(FakeLLMProvider):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            response = await super().complete(request)
            if (failure == "invalid_chunk" and len(self.requests) == 2) or (
                failure == "invalid_synthesis" and len(self.requests) == 3
            ):
                return LLMResponse(
                    content="not-json", provider="fake-provider", model="fake-model",
                    finish_reason="stop", usage=None,
                )
            return response

    provider = FailingProvider()
    sender = FakeQQOfficialSender()
    handler = SummaryCommandHandler(
        summary_service=SummaryService(
            query_service=ConversationQueryService(message_repository),
            renderer=MessageRenderer(), provider=provider,
            max_input_chars=800, chunk_messages=1,
        ),
        summary_repository=summary_repository, formatter=SummaryMessageFormatter(),
        sender=sender, enabled=True,
    )
    try:
        for index in range(2):
            raw = await raw_ingestion.ingest(dispatch(
                timestamp="2026-08-10T16:00:00+08:00",
                message_id=f"history-{index}",
                text="测试内容" * (300 if failure == "oversized" and index == 1 else 90),
            ))
            assert raw is not None
            assert await normalized_ingestion.ingest(raw) is not None
        command_raw = await raw_ingestion.ingest(dispatch(
            timestamp="2026-08-10T17:00:00+08:00", message_id="command-1", text="#总结",
        ))
        assert command_raw is not None
        command_message = await normalized_ingestion.ingest(command_raw)
        assert command_message is not None

        assert await handler.handle(command_message) is CommandStatus.GENERATION_FAILED
        assert sender.calls == []
        assert await summary_repository.list_for_group(platform="qq_official", group_id="group-1") == ()
        assert len(provider.requests) == {"oversized": 0, "invalid_chunk": 2, "invalid_synthesis": 3}[failure]
        async with database.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(RawEvent)) == 3
    finally:
        await database.dispose()


def dispatch(
    *,
    timestamp: str,
    message_id: str,
    text: str,
    attachments: list[dict] | None = None,
) -> QQGatewayDispatch:
    data = {
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
    }
    if attachments is not None:
        data["attachments"] = attachments
    return QQGatewayDispatch(
        sequence=1,
        event_type="GROUP_MESSAGE_CREATE",
        data=data,
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

        assert status is CommandStatus.SUCCEEDED
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


@pytest.mark.asyncio
async def test_image_history_is_enriched_between_query_and_summary_render(
    tmp_path: Path,
) -> None:
    class Resolver:
        async def resolve(self, image: ImageSegment) -> ResolvedImage:
            assert image.file == "traceback.png"
            return ResolvedImage(data=b"mock-png", content_type="image/png")

    class Vision:
        @property
        def supports_vision(self) -> bool:
            return True

        async def describe_image(
            self,
            image: ResolvedImage,
            *,
            prompt: str,
        ) -> ImageDescription:
            return ImageDescription(
                description="Python traceback showing ModuleNotFoundError for httpx"
            )

        async def aclose(self) -> None:
            return None

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'image-summary.db'}")
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
    handler = SummaryCommandHandler(
        summary_service=SummaryService(
            query_service=ConversationQueryService(message_repository),
            renderer=MessageRenderer(),
            provider=provider,
            image_enrichment_service=ImageEnrichmentService(
                enabled=True,
                resolver=Resolver(),
                provider=Vision(),
            ),
        ),
        summary_repository=SummaryRepository(database.session_factory),
        formatter=SummaryMessageFormatter(),
        sender=sender,
        enabled=True,
        lookback_minutes=120,
        cooldown_seconds=0,
    )
    try:
        history_raw = await raw_ingestion.ingest(
            dispatch(
                timestamp="2026-08-10T16:00:00+08:00",
                message_id="image-history",
                text="谁知道这个报错怎么解决？",
                attachments=[
                    {
                        "content_type": "image/png",
                        "filename": "traceback.png",
                        "height": 600,
                        "size": 123,
                        "url": "https://media.example.test/traceback.png",
                        "width": 800,
                    }
                ],
            )
        )
        command_raw = await raw_ingestion.ingest(
            dispatch(
                timestamp="2026-08-10T17:00:00+08:00",
                message_id="summary-command",
                text="#总结",
            )
        )
        assert history_raw is not None and command_raw is not None
        await normalized_ingestion.ingest(history_raw)
        command = await normalized_ingestion.ingest(command_raw)
        assert command is not None

        status = await handler.handle(command)

        assert status is CommandStatus.SUCCEEDED
        assert len(provider.requests) == 1
        prompt = provider.requests[0].user_prompt
        assert "谁知道这个报错怎么解决？" in prompt
        assert "ModuleNotFoundError" in prompt
        assert "media.example.test" not in prompt
        assert sender.calls[0][2] == "summary-command"
    finally:
        await database.dispose()
