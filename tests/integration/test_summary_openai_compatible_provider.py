from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
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
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.rendering import MessageRenderer
from app.services.summary import SummaryService


START = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)


class FakeQueryService:
    def __init__(self, messages: tuple[InternalMessage, ...]) -> None:
        self.messages = messages
        self.calls: list[dict[str, object]] = []

    async def get_messages(self, **kwargs: object):
        self.calls.append(kwargs)
        return self.messages[: kwargs["limit"]]  # type: ignore[index]


def message(text: str) -> InternalMessage:
    author = IdentityRef(
        platform="qq_official",
        user_id="member-1",
        display_name="测试成员",
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id="message-1",
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="group-openid-1",
        ),
        actor=author,
        author=author,
        timestamp=START,
        segments=(TextSegment(position=0, raw_data=None, text=text),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=1,
        ),
    )


@pytest.mark.asyncio
async def test_summary_service_accepts_openai_compatible_provider_without_provider_specific_logic() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.6-luna",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "群内确认了测试事项。",
                                    "topics": ["测试事项"],
                                    "key_points": ["继续执行"],
                                    "decisions": [],
                                    "action_items": [],
                                    "open_questions": [],
                                },
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(
            api_key="test-key",
            base_url="https://relay.example/v1",
            model="gpt-5.6-luna",
            client=client,
            max_retries=0,
        )
        query = FakeQueryService((message("测试消息"),))
        result = await SummaryService(
            query_service=query,  # type: ignore[arg-type]
            renderer=MessageRenderer(),
            provider=provider,
        ).summarize(
            platform="qq_official",
            group_id="group-openid-1",
            start_time=START,
            end_time=END,
        )
        await provider.aclose()

    assert len(captured) == 1
    assert captured[0].url == httpx.URL("https://relay.example/v1/chat/completions")
    assert len(query.calls) == 1
    assert result.summary == "群内确认了测试事项。"
    assert result.provider == "openai_compatible"
    assert result.model == "gpt-5.6-luna"
    assert result.message_count == 1
