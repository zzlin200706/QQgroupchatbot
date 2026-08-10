from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.llm import LLMRequest, LLMResponse
from app.parsers import OneBotMessageParser, QQOfficialMessageParser
from app.rendering import MessageRenderer
from app.services.conversation_query import ConversationQueryService
from app.services.raw_event_ingestion import RawEventIngestionService
from app.services.summary import SummaryService
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


QQ_SAMPLES = Path(__file__).parents[2] / "data" / "qq_official_samples"


class FakeLLMProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content=json.dumps(
                {
                    "summary": "安全集成摘要",
                    "topics": [],
                    "key_points": [],
                    "decisions": [],
                    "action_items": [],
                    "open_questions": [],
                },
                ensure_ascii=False,
            ),
            provider="fake",
            model="fake-model",
            finish_reason="stop",
            usage=None,
        )


def nested_onebot_event() -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 3001,
        "group_id": 2001,
        "user_id": 1001,
        "time": 1786320000,
        "sender": {"nickname": "Outer"},
        "message": [
            {
                "type": "forward",
                "data": {
                    "id": "outer-forward-private-id",
                    "content": [
                        {
                            "type": "node",
                            "data": {
                                "user_id": 2002,
                                "nickname": "B",
                                "content": [
                                    {"type": "text", "data": {"text": "hello"}}
                                ],
                            },
                        },
                        {
                            "type": "node",
                            "data": {
                                "content": [
                                    {
                                        "type": "forward",
                                        "data": {
                                            "id": "nested-forward-private-id",
                                            "content": [
                                                {
                                                    "type": "node",
                                                    "data": {
                                                        "user_id": 4004,
                                                        "nickname": "D",
                                                        "content": [
                                                            {
                                                                "type": "text",
                                                                "data": {"text": "nested"},
                                                            }
                                                        ],
                                                    },
                                                },
                                                {
                                                    "type": "node",
                                                    "data": {
                                                        "content": [
                                                            {
                                                                "type": "text",
                                                                "data": {"text": "old"},
                                                            }
                                                        ]
                                                    },
                                                },
                                            ],
                                        },
                                    }
                                ]
                            },
                        },
                    ],
                },
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected", "forbidden"),
    [
        (
            "onebot_nested",
            ("- B: hello", "[原作者不可用]:", "[合并转发]", "- D: nested"),
            ("outer-forward-private-id", "nested-forward-private-id", "2002", "4004"),
        ),
        (
            "qq_102",
            ("[合并转发：内容未解析]",),
            ("[发送者]", "multimedia.nt.qq.com.cn", "fileid="),
        ),
        (
            "qq_103",
            ("[回复消息，引用ID不可用]",),
            ("REFIDX_", "msg_elements", "message_scene"),
        ),
    ],
)
async def test_parser_storage_query_renderer_summary_pipeline(
    tmp_path: Path,
    scenario: str,
    expected: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> None:
    if scenario == "onebot_nested":
        platform = "onebot11"
        payload = nested_onebot_event()
        parsed_without_id = OneBotMessageParser().parse(
            payload,
            source_raw_event_id=1,
        )
    else:
        platform = "qq_official"
        filename = "012_merged_forward.json" if scenario == "qq_102" else "007_reply_text.json"
        payload = json.loads((QQ_SAMPLES / filename).read_text(encoding="utf-8"))
        parsed_without_id = QQOfficialMessageParser().parse(payload, raw_event_id=1)
    assert parsed_without_id is not None and parsed_without_id.timestamp is not None
    assert parsed_without_id.context.group_id is not None

    database = Database(f"sqlite+aiosqlite:///{tmp_path / f'{scenario}.db'}")
    await database.initialize()
    raw_repository = RawEventRepository(database.session_factory)
    message_repository = MessageRepository(database.session_factory)
    provider = FakeLLMProvider()
    try:
        raw = await raw_repository.insert(
            RawEvent(
                platform=platform,
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
        summary_service = SummaryService(
            query_service=ConversationQueryService(message_repository),
            renderer=MessageRenderer(),
            provider=provider,
        )

        result = await summary_service.summarize(
            platform=platform,
            group_id=parsed.context.group_id or "",
            start_time=parsed.timestamp - timedelta(seconds=1),
            end_time=parsed.timestamp + timedelta(seconds=1),
        )

        assert result.message_count == 1
        assert result.summary == "安全集成摘要"
        assert len(provider.requests) == 1
        prompt = provider.requests[0].user_prompt
        for marker in expected:
            assert marker in prompt
        for private_value in forbidden:
            assert private_value not in prompt
        assert parsed.context.group_id not in prompt
        assert "raw_payload" not in prompt
        assert "raw_data" not in prompt
    finally:
        await database.dispose()
