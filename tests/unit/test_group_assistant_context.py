from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.assistant_interactions import (
    AssistantInteraction,
    AssistantMode,
    AssistantResult,
    AssistantTriggerType,
    StoredAssistantInteraction,
)
from app.domain.messages import (
    ForwardResolutionStatus,
    ForwardSegment,
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
    ImageSegment,
    InternalMessage,
    MessageContext,
    MessageProvenance,
    ProvenanceSource,
    TextSegment,
)
from app.rendering import MessageRenderer
from app.services.group_assistant_context import GroupAssistantContextBuilder


BASE = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def identity(
    user_id: str | None = "user-a",
    *,
    availability: IdentityAvailability = IdentityAvailability.KNOWN,
) -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id=user_id,
        display_name="用户A" if user_id else None,
        card=None,
        source=IdentitySource.EVENT if user_id else IdentitySource.UNKNOWN,
        availability=availability,
    )


def message(
    text: str,
    *,
    timestamp: datetime,
    raw_event_id: int,
    author: IdentityRef | None = None,
    extra_segments: tuple[object, ...] = (),
) -> InternalMessage:
    person = author or identity()
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=raw_event_id,
        platform_message_id=f"message-{raw_event_id}",
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="group-a",
        ),
        actor=person,
        author=person,
        timestamp=timestamp,
        segments=(TextSegment(position=0, raw_data=text, text=text), *extra_segments),  # type: ignore[arg-type]
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=raw_event_id,
        ),
    )


def stored_interaction(*, timestamp: datetime) -> StoredAssistantInteraction:
    requester = identity()
    return StoredAssistantInteraction(
        id=7,
        created_at=timestamp + timedelta(seconds=1),
        interaction=AssistantInteraction(
            platform="qq_official",
            group_id="group-a",
            trigger_type=AssistantTriggerType.MENTION_CHAT,
            trigger_message_id="prior-trigger",
            trigger_timestamp=timestamp,
            requester=requester,
            question="先前问题",
            result=AssistantResult(
                answer="先前机器人回答",
                provider="fake",
                model="fake",
                finish_reason="stop",
                input_chars=10,
                prompt_version="chat-v1",
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
            ),
            response_message_id="bot-response",
        ),
    )


class FakeQuery:
    def __init__(self, messages: tuple[InternalMessage, ...]) -> None:
        self.messages = messages
        self.calls: list[dict[str, object]] = []

    async def get_messages_before(self, **kwargs: object) -> tuple[InternalMessage, ...]:
        self.calls.append(kwargs)
        return self.messages


class FakeInteractions:
    def __init__(self, interactions: tuple[StoredAssistantInteraction, ...]) -> None:
        self.interactions = interactions
        self.calls: list[dict[str, object]] = []

    async def list_recent_for_group(
        self,
        **kwargs: object,
    ) -> tuple[StoredAssistantInteraction, ...]:
        self.calls.append(kwargs)
        return self.interactions


def builder(
    query: FakeQuery,
    interactions: FakeInteractions,
) -> GroupAssistantContextBuilder:
    return GroupAssistantContextBuilder(
        query_service=query,  # type: ignore[arg-type]
        interaction_repository=interactions,  # type: ignore[arg-type]
        renderer=MessageRenderer(),
        qa_lookback_minutes=120,
        qa_max_messages=150,
        chat_lookback_minutes=30,
        chat_max_messages=80,
        chat_max_assistant_turns=20,
        max_input_chars=40000,
    )


@pytest.mark.asyncio
async def test_grounded_context_uses_only_group_messages_and_preserves_unknown_media_forward() -> None:
    unknown = identity(None, availability=IdentityAvailability.UNAVAILABLE)
    history = message(
        "明天不上课",
        timestamp=BASE - timedelta(minutes=5),
        raw_event_id=1,
        author=unknown,
        extra_segments=(
            ImageSegment(
                position=1,
                raw_data=None,
                file=None,
                url=None,
                summary=None,
                sub_type=None,
                file_size=None,
            ),
            ForwardSegment(
                position=2,
                raw_data=None,
                reference_id=None,
                resolved=False,
                resolution_status=ForwardResolutionStatus.UNRESOLVED,
                content=(),
                nodes=(),
            ),
        ),
    )
    query = FakeQuery((history,))
    interactions = FakeInteractions((stored_interaction(timestamp=BASE - timedelta(minutes=4)),))
    trigger = message("#问 谁说的？", timestamp=BASE, raw_event_id=9)

    context = await builder(query, interactions).build(
        mode=AssistantMode.GROUNDED_QA,
        trigger=trigger,
    )

    assert "[原作者不可用]" in context.rendered
    assert "[图片]" in context.rendered
    assert "[合并转发：内容未解析]" in context.rendered
    assert "先前机器人回答" not in context.rendered
    assert interactions.calls == []
    assert query.calls[0]["group_id"] == "group-a"
    assert query.calls[0]["trigger_raw_event_id"] == 9


@pytest.mark.asyncio
async def test_chat_context_merges_group_and_assistant_history_chronologically() -> None:
    first = message("第一条", timestamp=BASE - timedelta(minutes=4), raw_event_id=1)
    second = message("第二条", timestamp=BASE - timedelta(minutes=2), raw_event_id=2)
    prior = stored_interaction(timestamp=BASE - timedelta(minutes=3))
    query = FakeQuery((first, second))
    interactions = FakeInteractions((prior,))

    context = await builder(query, interactions).build(
        mode=AssistantMode.CHAT,
        trigger=message("现在的问题", timestamp=BASE, raw_event_id=9),
    )

    assert context.rendered.index("第一条") < context.rendered.index("先前机器人回答")
    assert context.rendered.index("先前机器人回答") < context.rendered.index("第二条")
    assert "机器人生成内容，不是群聊事实" in context.rendered
    assert interactions.calls[0]["group_id"] == "group-a"
    assert context.assistant_turn_count == 1


@pytest.mark.asyncio
async def test_duplicate_receipt_of_current_trigger_is_excluded_by_message_id() -> None:
    trigger = message("#问 几点？", timestamp=BASE, raw_event_id=9)
    duplicate = replace(
        message("#问 几点？", timestamp=BASE, raw_event_id=8),
        platform_message_id=trigger.platform_message_id,
    )
    query = FakeQuery((duplicate,))
    interactions = FakeInteractions(())

    context = await builder(query, interactions).build(
        mode=AssistantMode.GROUNDED_QA,
        trigger=trigger,
    )

    assert context.rendered == ""
    assert context.message_count == 0
