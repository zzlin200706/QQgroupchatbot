"""Deterministic assistant trigger detection over normalized messages."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.assistant_interactions import AssistantMode, AssistantTriggerType
from app.domain.messages import (
    AtSegment,
    FileSegment,
    ForwardSegment,
    ImageSegment,
    InternalMessage,
    ProvenanceSource,
    ReplySegment,
    TextSegment,
    UnknownSegment,
)


_QA_COMMAND = re.compile(r"^\s*#问(?:\s+(.*?))?\s*$", re.DOTALL)


@dataclass(frozen=True)
class GroupAssistantTrigger:
    trigger_type: AssistantTriggerType
    mode: AssistantMode
    user_input: str


def detect_group_assistant_trigger(
    message: InternalMessage,
) -> GroupAssistantTrigger | None:
    """Recognize only explicit grounded-QA or structurally verified bot mentions."""

    if message.platform != "qq_official":
        return None
    if message.provenance.source_type is not ProvenanceSource.DIRECT_EVENT:
        return None

    qa_input = _qa_input(message)
    if qa_input is not None:
        return GroupAssistantTrigger(
            trigger_type=AssistantTriggerType.GROUNDED_QA,
            mode=AssistantMode.GROUNDED_QA,
            user_input=qa_input,
        )

    if not _is_bot_mention(message):
        return None
    return GroupAssistantTrigger(
        trigger_type=AssistantTriggerType.MENTION_CHAT,
        mode=AssistantMode.CHAT,
        user_input=_mention_input(message),
    )


def _qa_input(message: InternalMessage) -> str | None:
    if not message.segments or any(
        not isinstance(segment, TextSegment) for segment in message.segments
    ):
        return None
    text = "".join(segment.text or "" for segment in message.segments)
    matched = _QA_COMMAND.fullmatch(text)
    if matched is None:
        return None
    return (matched.group(1) or "").strip()


def _is_bot_mention(message: InternalMessage) -> bool:
    if message.context.sub_type == "GROUP_AT_MESSAGE_CREATE":
        return True
    if message.context.sub_type != "GROUP_MESSAGE_CREATE":
        return False
    return any(
        isinstance(segment, AtSegment) and segment.is_self is True
        for segment in message.segments
    )


def _mention_input(message: InternalMessage) -> str:
    parts: list[str] = []
    for segment in sorted(message.segments, key=lambda value: value.position):
        if isinstance(segment, TextSegment):
            parts.append(segment.text or "")
        elif isinstance(segment, AtSegment):
            if segment.is_self is True:
                continue
            if segment.is_all:
                parts.append("@全体成员")
            else:
                parts.append("@" + (segment.display_name or "用户"))
        elif isinstance(segment, ImageSegment):
            parts.append("[图片]")
        elif isinstance(segment, FileSegment):
            parts.append(f"[文件: {segment.name}]" if segment.name else "[文件]")
        elif isinstance(segment, ReplySegment):
            parts.append("[回复消息，引用目标不可可靠识别]")
        elif isinstance(segment, ForwardSegment):
            parts.append("[合并转发：内容未解析]")
        elif isinstance(segment, UnknownSegment):
            parts.append("[未知消息类型]")
    return "".join(parts).strip()
