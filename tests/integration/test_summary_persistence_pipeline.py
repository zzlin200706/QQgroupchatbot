from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.domain.messages import InternalMessage
from app.llm import LLMRequest, LLMResponse, LLMServerError, LLMUsage
from app.parsers import OneBotMessageParser
from app.rendering import MessageRenderer
from app.services.conversation_query import ConversationQueryService
from app.services.raw_event_ingestion import RawEventIngestionService
from app.services.summary import SummaryService
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.models import RawEvent, SummaryRecord
from app.storage.raw_event_repository import RawEventRepository
from app.storage.summary_repository import SummaryRepository


ONEBOT_FIXTURE = Path(__file__).parents[1] / "fixtures" / "onebot" / "real_group_text_sanitized.json"


class FakeLLMProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.fail:
            raise LLMServerError("synthetic provider failure")
        return LLMResponse(
            content=json.dumps(
                {
                    "summary": "测试窗口摘要",
                    "topics": ["测试主题"],
                    "key_points": ["测试重点"],
                    "decisions": ["测试决定"],
                    "action_items": [
                        {
                            "description": "跟进测试事项",
                            "owner": None,
                            "deadline": None,
                        }
                    ],
                    "open_questions": ["待确认问题"],
                },
                ensure_ascii=False,
            ),
            provider="fake-provider",
            model="fake-model",
            finish_reason="stop",
            usage=LLMUsage(
                prompt_tokens=20,
                completion_tokens=10,
                total_tokens=30,
            ),
        )


async def prepare_pipeline(
    tmp_path: Path,
    *,
    fail_provider: bool = False,
) -> tuple[Database, SummaryService, SummaryRepository, InternalMessage]:
    payload = json.loads(ONEBOT_FIXTURE.read_text(encoding="utf-8"))
    parsed_without_id = OneBotMessageParser().parse(payload, source_raw_event_id=1)
    assert parsed_without_id is not None and parsed_without_id.timestamp is not None
    assert parsed_without_id.context.group_id is not None

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'summary-pipeline.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    message_repository = MessageRepository(database.session_factory)
    raw = await raw_repository.insert(
        RawEvent(
            platform="onebot11",
            received_at=parsed_without_id.timestamp,
            event_time=parsed_without_id.timestamp,
            post_type="message",
            message_type="group",
            sub_type=None,
            self_id=None,
            user_id=None,
            group_id=parsed_without_id.context.group_id,
            message_id=parsed_without_id.platform_message_id,
            raw_payload=payload,
            payload_hash=RawEventIngestionService.payload_hash(payload),
        )
    )
    parsed = OneBotMessageParser().parse(payload, source_raw_event_id=raw.id)
    assert parsed is not None and parsed.timestamp is not None
    await message_repository.persist(
        parsed,
        parser_name="onebot_message_parser",
        parser_version="1",
    )
    provider = FakeLLMProvider(fail=fail_provider)
    summary_service = SummaryService(
        query_service=ConversationQueryService(message_repository),
        renderer=MessageRenderer(),
        provider=provider,
    )
    return (
        database,
        summary_service,
        SummaryRepository(database.session_factory),
        parsed,
    )


@pytest.mark.asyncio
async def test_validated_summary_round_trips_through_historical_storage(
    tmp_path: Path,
) -> None:
    database, summary_service, summary_repository, parsed = await prepare_pipeline(
        tmp_path
    )
    try:
        result = await summary_service.summarize(
            platform=parsed.platform,
            group_id=parsed.context.group_id,
            start_time=parsed.timestamp - timedelta(seconds=1),
            end_time=parsed.timestamp + timedelta(seconds=1),
        )
        stored = await summary_repository.persist(result)
        loaded = await summary_repository.get_by_id(stored.id)
        history = await summary_repository.list_for_group(
            platform=parsed.platform,
            group_id=parsed.context.group_id,
        )

        assert loaded == stored
        assert stored.result == result
        assert stored.result.action_items[0].owner is None
        assert stored.result.action_items[0].deadline is None
        assert stored.result.prompt_tokens == 20
        assert stored.result.completion_tokens == 10
        assert stored.result.total_tokens == 30
        assert history == (stored,)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_provider_failure_never_creates_a_summary_row(tmp_path: Path) -> None:
    database, summary_service, _, parsed = await prepare_pipeline(
        tmp_path,
        fail_provider=True,
    )
    try:
        with pytest.raises(LLMServerError):
            await summary_service.summarize(
                platform=parsed.platform,
                group_id=parsed.context.group_id,
                start_time=parsed.timestamp - timedelta(seconds=1),
                end_time=parsed.timestamp + timedelta(seconds=1),
            )

        async with database.session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(SummaryRecord))
        assert count == 0
    finally:
        await database.dispose()
