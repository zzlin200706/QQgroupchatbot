"""Tests for the layered, bounded assistant context builder."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.domain.assistant_interactions import (
    AssistantInteraction,
    AssistantResult,
    AssistantTriggerType,
    StoredAssistantInteraction,
)
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
from app.rendering import MessageRenderer
from app.services.conversation_query import ConversationQueryService
from app.services.layered_context import (
    AssistantContextTooLarge,
    LayeredContextBuilder,
)
from app.storage.assistant_interaction_repository import (
    AssistantInteractionRepository,
)
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


async def _persist_message(env, message: InternalMessage) -> None:
    """Insert a raw_event + message row pair to satisfy the FK constraint."""
    raw = RawEvent(
        platform=message.platform,
        received_at=datetime.now(timezone.utc),
        event_time=message.timestamp,
        post_type=None,
        message_type="0",
        sub_type="GROUP_MESSAGE_CREATE",
        self_id=None,
        user_id=message.author.user_id,
        group_id=message.context.group_id,
        message_id=message.platform_message_id,
        raw_payload={},
        payload_hash=f"h-{message.platform_message_id}",
    )
    persisted_raw = await env["raw_event_repo"].insert(raw)
    # Rewrite the source_raw_event_id so the FK lines up.
    message_with_raw = message.__class__(
        **{**message.__dict__, "source_raw_event_id": persisted_raw.id}
    )
    await env["message_repo"].persist(
        message_with_raw, parser_name="p", parser_version="1"
    )


def _identity(uid: str = "user-1") -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id=uid,
        display_name=uid,
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )


def _message(*, text: str, message_id: str, ts: datetime, source_raw_event_id: int) -> InternalMessage:
    actor = _identity()
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=source_raw_event_id,
        platform_message_id=message_id,
        context=MessageContext(
            message_type="0", sub_type="GROUP_MESSAGE_CREATE", group_id="g-1"
        ),
        actor=actor,
        author=actor,
        timestamp=ts,
        segments=(TextSegment(position=0, raw_data=text, text=text),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT, raw_event_id=source_raw_event_id
        ),
    )


def _stored_assistant(ts: datetime, answer: str = "ok") -> StoredAssistantInteraction:
    interaction = AssistantInteraction(
        platform="qq_official",
        group_id="g-1",
        trigger_type=AssistantTriggerType.MENTION_CHAT,
        trigger_message_id="bot-1",
        trigger_timestamp=ts,
        requester=_identity("requester"),
        question="?",
        result=AssistantResult(
            answer=answer,
            provider="openai_compatible",
            model="m",
            finish_reason="stop",
            input_chars=10,
            prompt_version="v1",
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
        ),
        response_message_id="bot-resp-1",
    )
    return StoredAssistantInteraction(id=1, created_at=ts, interaction=interaction)


class _StaticSummary:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def latest_for_group(self, *, platform: str, group_id: str) -> str | None:
        return self._value


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'ctx.db'}")
    await db.initialize()
    yield {
        "db": db,
        "message_repo": MessageRepository(db.session_factory),
        "interaction_repo": AssistantInteractionRepository(db.session_factory),
        "raw_event_repo": RawEventRepository(db.session_factory),
    }
    await db.dispose()


@pytest.mark.asyncio
async def test_no_messages_no_summary_returns_current_only(env) -> None:
    builder = LayeredContextBuilder(
        query_service=ConversationQueryService(env["message_repo"]),
        interaction_repository=env["interaction_repo"],
        renderer=MessageRenderer(),
        summary_provider=_StaticSummary(None),
        lookback_minutes=10,
        max_messages=10,
        max_assistant_turns=0,
        max_input_chars=20000,
    )
    trigger_ts = datetime.now(timezone.utc)
    trigger = _message(text="hello", message_id="trigger", ts=trigger_ts, source_raw_event_id=1)
    ctx = await builder.build(mode="chat", trigger=trigger)
    assert ctx.message_count == 0
    assert ctx.assistant_turn_count == 0
    assert ctx.dropped_layers == ()
    assert "当前用户请求" in ctx.rendered
    assert "hello" in ctx.rendered


@pytest.mark.asyncio
async def test_recent_messages_are_included(env) -> None:
    base = datetime.now(timezone.utc)
    # Persist three earlier messages, then the trigger.
    for index, text in enumerate(["a", "b", "c"]):
        await _persist_message(
            env,
            _message(
                text=text,
                message_id=f"m-{index}",
                ts=base - timedelta(seconds=10 - index),
                source_raw_event_id=index + 1,
            ),
        )
    builder = LayeredContextBuilder(
        query_service=ConversationQueryService(env["message_repo"]),
        interaction_repository=env["interaction_repo"],
        renderer=MessageRenderer(),
        lookback_minutes=10,
        max_messages=10,
        max_assistant_turns=0,
        max_input_chars=20000,
    )
    trigger = _message(
        text="trigger", message_id="trigger", ts=base, source_raw_event_id=99
    )
    ctx = await builder.build(mode="chat", trigger=trigger)
    assert ctx.message_count == 3
    # Order is deterministic; older messages appear first.
    rendered = ctx.rendered
    pos_a = rendered.index("a")
    pos_b = rendered.index("b")
    pos_c = rendered.index("c")
    assert pos_a < pos_b < pos_c
    assert "当前用户请求" in rendered


@pytest.mark.asyncio
async def test_rolling_summary_layer_is_included_when_present(env) -> None:
    builder = LayeredContextBuilder(
        query_service=ConversationQueryService(env["message_repo"]),
        interaction_repository=env["interaction_repo"],
        renderer=MessageRenderer(),
        summary_provider=_StaticSummary("过去30分钟大家在聊X话题"),
        lookback_minutes=10,
        max_messages=10,
        max_assistant_turns=0,
        max_input_chars=20000,
    )
    trigger = _message(text="hi", message_id="t", ts=datetime.now(timezone.utc), source_raw_event_id=1)
    ctx = await builder.build(mode="chat", trigger=trigger)
    assert "过去30分钟" in ctx.rendered
    assert ctx.rolling_summary_chars > 0


@pytest.mark.asyncio
async def test_assistant_turns_dropped_first_when_over_budget(env) -> None:
    base = datetime.now(timezone.utc)
    interaction = AssistantInteraction(
        platform="qq_official",
        group_id="g-1",
        trigger_type=AssistantTriggerType.MENTION_CHAT,
        trigger_message_id="bot-1",
        trigger_timestamp=base - timedelta(seconds=30),
        requester=_identity("requester"),
        question="?",
        result=AssistantResult(
            answer="x" * 4000,
            provider="openai_compatible",
            model="m",
            finish_reason="stop",
            input_chars=10,
            prompt_version="v1",
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
        ),
        response_message_id="bot-resp-1",
    )
    await env["interaction_repo"].insert_successful_interaction(interaction)
    builder = LayeredContextBuilder(
        query_service=ConversationQueryService(env["message_repo"]),
        interaction_repository=env["interaction_repo"],
        renderer=MessageRenderer(),
        summary_provider=_StaticSummary("摘要 " * 200),
        lookback_minutes=10,
        max_messages=10,
        max_assistant_turns=1,
        # Small enough that not all layers fit.
        max_input_chars=500,
    )
    trigger = _message(text="hi", message_id="t", ts=base, source_raw_event_id=1)
    ctx = await builder.build(mode="chat", trigger=trigger)
    assert "assistant_turns" in ctx.dropped_layers
    # The bot's old reply is NOT in the context.
    assert "x" * 4000 not in ctx.rendered
    # The current request is always present.
    assert "hi" in ctx.rendered


@pytest.mark.asyncio
async def test_rolling_summary_dropped_after_assistant_turns(env) -> None:
    builder = LayeredContextBuilder(
        query_service=ConversationQueryService(env["message_repo"]),
        interaction_repository=env["interaction_repo"],
        renderer=MessageRenderer(),
        summary_provider=_StaticSummary("S" * 5000),
        lookback_minutes=10,
        max_messages=10,
        max_assistant_turns=0,
        max_input_chars=200,
    )
    trigger = _message(text="hi", message_id="t", ts=datetime.now(timezone.utc), source_raw_event_id=1)
    ctx = await builder.build(mode="chat", trigger=trigger)
    assert "rolling_summary" in ctx.dropped_layers
    # Summary not in the context.
    assert "S" * 5000 not in ctx.rendered


@pytest.mark.asyncio
async def test_raises_when_messages_plus_current_exceed_budget(env) -> None:
    base = datetime.now(timezone.utc)
    for index in range(5):
        await _persist_message(
            env,
            _message(
                text="x" * 200,
                message_id=f"m-{index}",
                ts=base - timedelta(seconds=10 - index),
                source_raw_event_id=index + 1,
            ),
        )
    builder = LayeredContextBuilder(
        query_service=ConversationQueryService(env["message_repo"]),
        interaction_repository=env["interaction_repo"],
        renderer=MessageRenderer(),
        lookback_minutes=10,
        max_messages=10,
        max_assistant_turns=0,
        max_input_chars=10,  # absurdly small
    )
    trigger = _message(
        text="trigger", message_id="t", ts=base, source_raw_event_id=99
    )
    with pytest.raises(AssistantContextTooLarge):
        await builder.build(mode="chat", trigger=trigger)


@pytest.mark.asyncio
async def test_chat_mode_includes_assistant_turns_qa_mode_does_not(env) -> None:
    base = datetime.now(timezone.utc)
    interaction = AssistantInteraction(
        platform="qq_official",
        group_id="g-1",
        trigger_type=AssistantTriggerType.MENTION_CHAT,
        trigger_message_id="bot-1",
        trigger_timestamp=base - timedelta(seconds=30),
        requester=_identity("requester"),
        question="?",
        result=AssistantResult(
            answer="x" * 200,
            provider="openai_compatible",
            model="m",
            finish_reason="stop",
            input_chars=10,
            prompt_version="v1",
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
        ),
        response_message_id="bot-resp-1",
    )
    await env["interaction_repo"].insert_successful_interaction(interaction)
    builder = LayeredContextBuilder(
        query_service=ConversationQueryService(env["message_repo"]),
        interaction_repository=env["interaction_repo"],
        renderer=MessageRenderer(),
        lookback_minutes=10,
        max_messages=10,
        max_assistant_turns=1,
        max_input_chars=20000,
    )
    trigger = _message(
        text="hi", message_id="t", ts=base, source_raw_event_id=1
    )
    chat_ctx = await builder.build(mode="chat", trigger=trigger)
    assert chat_ctx.assistant_turn_count == 1
    qa_ctx = await builder.build(mode="grounded_qa", trigger=trigger)
    assert qa_ctx.assistant_turn_count == 0
