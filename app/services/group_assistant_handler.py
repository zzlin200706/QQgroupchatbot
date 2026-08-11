"""QQ orchestration for explicit group assistant interactions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from enum import Enum
from typing import Protocol

from app.domain.assistant_interactions import (
    AssistantInteraction,
    AssistantResult,
    AssistantTriggerType,
)
from app.domain.messages import (
    AtSegment,
    FileSegment,
    ForwardSegment,
    ImageSegment,
    InternalMessage,
    ReplySegment,
    TextSegment,
    UnknownSegment,
)
from app.services.group_assistant_trigger import (
    GroupAssistantTrigger,
    detect_group_assistant_trigger,
)
from app.storage.assistant_interaction_repository import AssistantInteractionRepository


logger = logging.getLogger(__name__)

ASSISTANT_FAILURE_MESSAGE = "暂时无法回答，请稍后再试。"
QA_USAGE_MESSAGE = "用法：#问 你的问题"
MENTION_USAGE_MESSAGE = "请在 @机器人 后输入问题。"


class GroupAssistantStatus(str, Enum):
    DISABLED = "disabled"
    NOT_TRIGGER = "not_trigger"
    INVALID_CONTEXT = "invalid_context"
    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"
    USAGE_SENT = "usage_sent"
    GENERATION_FAILED = "generation_failed"
    SEND_FAILED = "send_failed"
    PERSISTENCE_FAILED = "persistence_failed"
    SUCCEEDED = "succeeded"


class _AssistantService(Protocol):
    async def answer(self, **kwargs: object) -> AssistantResult: ...


class _GroupMessageSender(Protocol):
    async def send_group_message(
        self,
        group_id: str,
        message: str,
        *,
        msg_id: str | None = None,
    ) -> object: ...


class GroupAssistantHandler:
    """Generate, send, then persist one successful assistant turn."""

    def __init__(
        self,
        *,
        service: _AssistantService,
        repository: AssistantInteractionRepository,
        sender: _GroupMessageSender,
        enabled: bool,
        cooldown_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        self._service = service
        self._repository = repository
        self._sender = sender
        self._enabled = enabled
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._state_lock = asyncio.Lock()
        self._last_started: dict[tuple[str, str, str], float] = {}

    def detect(self, message: InternalMessage) -> GroupAssistantTrigger | None:
        if not self._enabled:
            return None
        return detect_group_assistant_trigger(message)

    async def handle(self, message: InternalMessage) -> GroupAssistantStatus:
        if not self._enabled:
            return GroupAssistantStatus.DISABLED
        trigger = detect_group_assistant_trigger(message)
        unverified_platform_quote = (
            _quoted_platform_content(message)
            if trigger is not None
            and trigger.trigger_type is AssistantTriggerType.MENTION_CHAT
            else None
        )
        if trigger is None:
            return GroupAssistantStatus.NOT_TRIGGER
        if not self._valid_context(message):
            return GroupAssistantStatus.INVALID_CONTEXT

        group_id = message.context.group_id
        message_id = message.platform_message_id
        timestamp = message.timestamp
        assert group_id is not None
        assert message_id is not None
        assert timestamp is not None
        try:
            claimed = await self._repository.claim_trigger(
                platform=message.platform,
                group_id=group_id,
                trigger_message_id=message_id,
                trigger_type=trigger.trigger_type,
            )
        except Exception as error:
            logger.warning(
                "assistant trigger claim failed group_id=%s trigger_type=%s error_type=%s",
                group_id,
                trigger.trigger_type.value,
                type(error).__name__,
            )
            return GroupAssistantStatus.PERSISTENCE_FAILED
        if not claimed:
            return GroupAssistantStatus.DUPLICATE

        if not trigger.user_input:
            usage = (
                QA_USAGE_MESSAGE
                if trigger.trigger_type is AssistantTriggerType.GROUNDED_QA
                else MENTION_USAGE_MESSAGE
            )
            return await self._send_usage(
                group_id=group_id,
                message_id=message_id,
                usage=usage,
            )

        requester_key = message.actor.user_id or "<unknown>"
        if not await self._claim_cooldown(
            (message.platform, group_id, requester_key)
        ):
            return GroupAssistantStatus.COOLDOWN

        try:
            result = await self._service.answer(
                mode=trigger.mode,
                message=message,
                user_input=trigger.user_input,
                quoted_platform_content=unverified_platform_quote,
            )
        except Exception as error:
            logger.warning(
                "assistant generation failed group_id=%s trigger_type=%s error_type=%s",
                group_id,
                trigger.trigger_type.value,
                type(error).__name__,
            )
            await self._send_safe_failure(group_id=group_id, message_id=message_id)
            return GroupAssistantStatus.GENERATION_FAILED

        try:
            send_result = await self._sender.send_group_message(
                group_id,
                result.answer,
                msg_id=message_id,
            )
        except Exception as error:
            logger.warning(
                "assistant send failed group_id=%s trigger_type=%s error_type=%s",
                group_id,
                trigger.trigger_type.value,
                type(error).__name__,
            )
            return GroupAssistantStatus.SEND_FAILED

        interaction = AssistantInteraction(
            platform=message.platform,
            group_id=group_id,
            trigger_type=trigger.trigger_type,
            trigger_message_id=message_id,
            trigger_timestamp=timestamp,
            requester=message.actor,
            question=trigger.user_input,
            result=result,
            response_message_id=_response_message_id(send_result),
        )
        try:
            await self._repository.insert_successful_interaction(interaction)
        except Exception as error:
            logger.warning(
                "assistant persistence failed group_id=%s trigger_type=%s error_type=%s",
                group_id,
                trigger.trigger_type.value,
                type(error).__name__,
            )
            return GroupAssistantStatus.PERSISTENCE_FAILED
        logger.info(
            "assistant interaction succeeded group_id=%s trigger_type=%s provider=%s",
            group_id,
            trigger.trigger_type.value,
            result.provider,
        )
        return GroupAssistantStatus.SUCCEEDED

    async def _send_usage(
        self,
        *,
        group_id: str,
        message_id: str,
        usage: str,
    ) -> GroupAssistantStatus:
        try:
            await self._sender.send_group_message(group_id, usage, msg_id=message_id)
            return GroupAssistantStatus.USAGE_SENT
        except Exception as error:
            logger.warning(
                "assistant usage send failed group_id=%s error_type=%s",
                group_id,
                type(error).__name__,
            )
            return GroupAssistantStatus.SEND_FAILED

    async def _send_safe_failure(self, *, group_id: str, message_id: str) -> None:
        try:
            await self._sender.send_group_message(
                group_id,
                ASSISTANT_FAILURE_MESSAGE,
                msg_id=message_id,
            )
        except Exception as error:
            logger.warning(
                "assistant failure reply failed group_id=%s error_type=%s",
                group_id,
                type(error).__name__,
            )

    async def _claim_cooldown(self, key: tuple[str, str, str]) -> bool:
        async with self._state_lock:
            now = self._clock()
            previous = self._last_started.get(key)
            if (
                previous is not None
                and now - previous < self._cooldown_seconds
            ):
                return False
            self._last_started[key] = now
            return True

    @staticmethod
    def _valid_context(message: InternalMessage) -> bool:
        if message.context.sub_type not in {
            "GROUP_MESSAGE_CREATE",
            "GROUP_AT_MESSAGE_CREATE",
        }:
            return False
        if not message.context.group_id or not message.platform_message_id:
            return False
        timestamp = message.timestamp
        return (
            timestamp is not None
            and timestamp.tzinfo is not None
            and timestamp.utcoffset() is not None
        )


def _response_message_id(send_result: object) -> str | None:
    value = getattr(send_result, "message_id", None)
    return value if isinstance(value, str) and value.strip() else None


def _quoted_platform_content(message: InternalMessage) -> str | None:
    replies = tuple(
        segment for segment in message.segments if isinstance(segment, ReplySegment)
    )
    if len(replies) != 1 or replies[0].resolved_message is None:
        return None
    parts: list[str] = []
    for segment in sorted(
        replies[0].resolved_message.segments,
        key=lambda value: value.position,
    ):
        if isinstance(segment, TextSegment):
            parts.append(segment.text or "")
        elif isinstance(segment, AtSegment):
            parts.append("@" + (segment.display_name or "用户"))
        elif isinstance(segment, ImageSegment):
            parts.append("[图片]")
        elif isinstance(segment, FileSegment):
            parts.append(f"[文件: {segment.name}]" if segment.name else "[文件]")
        elif isinstance(segment, ForwardSegment):
            parts.append("[合并转发：内容未解析]")
        elif isinstance(segment, ReplySegment):
            parts.append("[引用消息]")
        elif isinstance(segment, UnknownSegment):
            parts.append("[未知消息类型]")
    rendered = "".join(parts).strip()
    return rendered or None
