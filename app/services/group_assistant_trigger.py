"""Deterministic assistant trigger detection over normalized messages.

Reply policy (v1.1, every rule has an explicit test):

1. ``#问 ...`` exact top-level text  -> GROUNDED_QA
2. Direct bot mention (``@bot``)     -> MENTION_CHAT
3. Reply to one of the bot's own
   previous messages                  -> REPLY_TO_BOT
4. Anything else                       -> no trigger (silent)

GROUNDED_QA has strict priority over MENTION_CHAT, which has
strict priority over REPLY_TO_BOT.  Reply-to-bot detection is
opt-in via ``bot_user_id``: when the operator has not configured
a bot identity the function falls back to the v1.0 behaviour
(strict mention only) so existing deployments do not silently
start answering implicit replies.
"""

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
    ReplySegment,
    ResolvedMessageReference,
    TextSegment,
    UnknownSegment,
)
from app.domain.messages.models import ProvenanceSource
from app.rendering import MessageRenderer


_QA_COMMAND = re.compile(r"^\s*#问(?:\s+(.*?))?\s*$", re.DOTALL)


@dataclass(frozen=True)
class GroupAssistantTrigger:
    trigger_type: AssistantTriggerType
    mode: AssistantMode
    user_input: str


def detect_group_assistant_trigger(
    message: InternalMessage,
    *,
    bot_user_id: str | None = None,
) -> GroupAssistantTrigger | None:
    """Recognize only explicit grounded-QA, bot mention, or reply-to-bot."""

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

    if _is_bot_mention(message):
        return GroupAssistantTrigger(
            trigger_type=AssistantTriggerType.MENTION_CHAT,
            mode=AssistantMode.CHAT,
            user_input=_mention_input(message),
        )

    if _is_reply_to_bot(message, bot_user_id=bot_user_id):
        return GroupAssistantTrigger(
            trigger_type=AssistantTriggerType.REPLY_TO_BOT,
            mode=AssistantMode.CHAT,
            user_input=_reply_input(message),
        )

    return None


def _qa_input(message: InternalMessage) -> str | None:
    if not message.segments or any(
        not isinstance(segment, (TextSegment, ReplySegment))
        for segment in message.segments
    ):
        return None
    text = "".join(
        segment.text or ""
        for segment in message.segments
        if isinstance(segment, TextSegment)
    )
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


def _is_reply_to_bot(
    message: InternalMessage, *, bot_user_id: str | None
) -> bool:
    """True if the message is a reply to a previous bot message.

    A reply is only eligible when (a) the bot identity is known
    (``bot_user_id`` is not empty), (b) the message contains a
    top-level ``ReplySegment`` with a resolved message reference,
    and (c) that referenced message was authored by the bot.
    """

    if not bot_user_id:
        return False
    return any(
        _reply_targets_bot(segment, bot_user_id=bot_user_id)
        for segment in message.segments
        if isinstance(segment, ReplySegment)
    )


def _reply_targets_bot(
    segment: ReplySegment, *, bot_user_id: str
) -> bool:
    resolved: ResolvedMessageReference | None = segment.resolved_message
    if resolved is None:
        return False
    return resolved.author.user_id == bot_user_id


def _mention_input(message: InternalMessage) -> str:
    return _render_visible_segments(message)


def _reply_input(message: InternalMessage) -> str:
    return _render_visible_segments(message)


def _render_visible_segments(message: InternalMessage) -> str:
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
            description = segment.description or segment.summary
            parts.append(
                f"[图片描述（AI生成，未独立验证）: {description}]"
                if description
                else "[图片]"
            )
        elif isinstance(segment, FileSegment):
            parts.append(f"[文件: {segment.name}]" if segment.name else "[文件]")
        elif isinstance(segment, ReplySegment):
            continue
        elif isinstance(segment, ForwardSegment):
            parts.append(
                MessageRenderer().render_segments((segment,), platform=message.platform)
            )
        elif isinstance(segment, UnknownSegment):
            parts.append("[未知消息类型]")
    return "".join(parts).strip()
