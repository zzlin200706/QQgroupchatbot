from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.domain.assistant_interactions import AssistantMode, AssistantTriggerType
from app.domain.messages import (
    AtSegment,
    ForwardResolutionStatus,
    ForwardSegment,
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
    InternalMessage,
    ImageSegment,
    MessageContext,
    MessageProvenance,
    ProvenanceSource,
    ReplySegment,
    TextSegment,
)
from app.parsers.qq_official_message_parser import QQOfficialMessageParser
from app.services.group_assistant_trigger import detect_group_assistant_trigger


SAMPLES = Path(__file__).parents[2] / "data" / "qq_official_samples"


def message(
    *segments: object,
    sub_type: str = "GROUP_MESSAGE_CREATE",
    message_type: str = "0",
) -> InternalMessage:
    identity = IdentityRef(
        platform="qq_official",
        user_id="user-1",
        display_name="用户一",
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id="message-1",
        context=MessageContext(
            message_type=message_type,
            sub_type=sub_type,
            group_id="group-1",
        ),
        actor=identity,
        author=identity,
        timestamp=datetime(2026, 8, 11, tzinfo=timezone.utc),
        segments=segments,  # type: ignore[arg-type]
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=1,
        ),
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("#问 明天几点？", "明天几点？"),
        ("  #问   明天几点？   ", "明天几点？"),
        ("#问", ""),
        (" #问 ", ""),
    ],
)
def test_grounded_qa_command_is_exact_and_extracts_question(
    text: str,
    expected: str,
) -> None:
    trigger = detect_group_assistant_trigger(
        message(TextSegment(position=0, raw_data=text, text=text))
    )

    assert trigger is not None
    assert trigger.mode is AssistantMode.GROUNDED_QA
    assert trigger.trigger_type is AssistantTriggerType.GROUNDED_QA
    assert trigger.user_input == expected


@pytest.mark.parametrize(
    "text",
    ["#问题", "#问问", "我想#问", "今天聊聊#问", "ping", "你好", "今天吃什么"],
)
def test_non_commands_are_not_grounded_qa(text: str) -> None:
    assert (
        detect_group_assistant_trigger(
            message(TextSegment(position=0, raw_data=text, text=text))
        )
        is None
    )


def test_group_at_event_is_structural_chat_trigger() -> None:
    trigger = detect_group_assistant_trigger(
        message(
            TextSegment(position=0, raw_data=" Python GIL 是什么？", text=" Python GIL 是什么？"),
            sub_type="GROUP_AT_MESSAGE_CREATE",
        )
    )

    assert trigger is not None
    assert trigger.mode is AssistantMode.CHAT
    assert trigger.user_input == "Python GIL 是什么？"


def test_captured_group_message_bot_mention_is_detected_and_removed() -> None:
    payload = json.loads((SAMPLES / "002_at_bot.json").read_text(encoding="utf-8"))
    parsed = QQOfficialMessageParser().parse(payload, raw_event_id=1)
    assert parsed is not None

    trigger = detect_group_assistant_trigger(parsed)

    assert trigger is not None
    assert trigger.trigger_type is AssistantTriggerType.MENTION_CHAT
    assert trigger.user_input == "phase-d-at-bot-002"


def test_other_member_mention_is_preserved_but_does_not_trigger_alone() -> None:
    payload = json.loads((SAMPLES / "003_at_member.json").read_text(encoding="utf-8"))
    parsed = QQOfficialMessageParser().parse(payload, raw_event_id=1)
    assert parsed is not None
    assert detect_group_assistant_trigger(parsed) is None

    trigger = detect_group_assistant_trigger(
        message(
            AtSegment(
                position=0,
                raw_data=None,
                target="bot-id",
                is_all=False,
                display_name="机器人",
                is_self=True,
            ),
            AtSegment(
                position=1,
                raw_data=None,
                target="member-id",
                is_all=False,
                display_name="小明",
                is_self=False,
            ),
            TextSegment(position=2, raw_data=" 你觉得他说得对吗？", text=" 你觉得他说得对吗？"),
        )
    )
    assert trigger is not None
    assert trigger.user_input == "@小明 你觉得他说得对吗？"


def test_opaque_reply_is_not_assumed_to_target_bot() -> None:
    reply = ReplySegment(
        position=0,
        raw_data={"ref_msg_idx": "opaque"},
        referenced_message_id=None,
        reference_key="REFIDX_opaque==",
    )
    assert (
        detect_group_assistant_trigger(
            message(
                reply,
                TextSegment(position=1, raw_data="继续说", text="继续说"),
                message_type="103",
            )
        )
        is None
    )


def test_grounded_qa_has_precedence_over_reply_reference() -> None:
    trigger = detect_group_assistant_trigger(
        message(
            ReplySegment(
                position=0,
                raw_data={"msg_idx": "REFIDX_bot=="},
                referenced_message_id=None,
                reference_key="REFIDX_bot==",
            ),
            TextSegment(position=1, raw_data="#问 谁说的？", text="#问 谁说的？"),
            message_type="103",
        )
    )
    assert trigger is not None
    assert trigger.trigger_type is AssistantTriggerType.GROUNDED_QA
    assert trigger.user_input == "谁说的？"


@pytest.mark.parametrize(
    "segment",
    [
        ImageSegment(
            position=0,
            raw_data=None,
            file=None,
            url=None,
            summary=None,
            sub_type=None,
            file_size=None,
        ),
        ForwardSegment(
            position=0,
            raw_data=None,
            reference_id=None,
            resolved=False,
            resolution_status=ForwardResolutionStatus.UNRESOLVED,
            content=(),
            nodes=(),
        ),
    ],
)
def test_ordinary_media_or_forward_is_not_an_assistant_trigger(segment: object) -> None:
    assert detect_group_assistant_trigger(message(segment)) is None
