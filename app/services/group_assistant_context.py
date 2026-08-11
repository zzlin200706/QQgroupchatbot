"""Bounded, group-scoped context assembly for assistant interactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.assistant_interactions import AssistantMode, StoredAssistantInteraction
from app.domain.messages import InternalMessage
from app.rendering import MessageRenderer
from app.services.conversation_query import ConversationQueryService
from app.storage.assistant_interaction_repository import AssistantInteractionRepository


class AssistantContextTooLarge(ValueError):
    """Rendered assistant input exceeded the configured product bound."""


@dataclass(frozen=True)
class GroupAssistantContext:
    rendered: str
    message_count: int
    assistant_turn_count: int
    input_chars: int


class GroupAssistantContextBuilder:
    """Query trusted group messages and successful assistant turns."""

    def __init__(
        self,
        *,
        query_service: ConversationQueryService,
        interaction_repository: AssistantInteractionRepository,
        renderer: MessageRenderer,
        qa_lookback_minutes: int,
        qa_max_messages: int,
        chat_lookback_minutes: int,
        chat_max_messages: int,
        chat_max_assistant_turns: int,
        max_input_chars: int,
    ) -> None:
        if renderer.max_chars is not None:
            raise ValueError("assistant renderer must not truncate message facts")
        for name, value in (
            ("qa_lookback_minutes", qa_lookback_minutes),
            ("qa_max_messages", qa_max_messages),
            ("chat_lookback_minutes", chat_lookback_minutes),
            ("chat_max_messages", chat_max_messages),
            ("chat_max_assistant_turns", chat_max_assistant_turns),
            ("max_input_chars", max_input_chars),
        ):
            if value < 1:
                raise ValueError(f"{name} must be at least one")
        self._query_service = query_service
        self._interaction_repository = interaction_repository
        self._renderer = renderer
        self._qa_lookback_minutes = qa_lookback_minutes
        self._qa_max_messages = qa_max_messages
        self._chat_lookback_minutes = chat_lookback_minutes
        self._chat_max_messages = chat_max_messages
        self._chat_max_assistant_turns = chat_max_assistant_turns
        self._max_input_chars = max_input_chars

    async def build(
        self,
        *,
        mode: AssistantMode,
        trigger: InternalMessage,
    ) -> GroupAssistantContext:
        timestamp = trigger.timestamp
        group_id = trigger.context.group_id
        if timestamp is None or group_id is None:
            raise ValueError("assistant trigger requires timestamp and group_id")
        if mode is AssistantMode.GROUNDED_QA:
            lookback_minutes = self._qa_lookback_minutes
            max_messages = self._qa_max_messages
        else:
            lookback_minutes = self._chat_lookback_minutes
            max_messages = self._chat_max_messages

        queried_messages = tuple(
            await self._query_service.get_messages_before(
                platform=trigger.platform,
                group_id=group_id,
                start_time=timestamp - timedelta(minutes=lookback_minutes),
                trigger_time=timestamp,
                trigger_raw_event_id=trigger.source_raw_event_id,
                limit=max_messages,
            )
        )
        messages = tuple(
            candidate
            for candidate in queried_messages
            if candidate.platform_message_id != trigger.platform_message_id
        )
        interactions: tuple[StoredAssistantInteraction, ...] = ()
        if mode is AssistantMode.CHAT:
            interactions = tuple(
                await self._interaction_repository.list_recent_for_group(
                    platform=trigger.platform,
                    group_id=group_id,
                    start_time=timestamp - timedelta(minutes=lookback_minutes),
                    before_time=timestamp,
                    limit=self._chat_max_assistant_turns,
                )
            )

        rendered = self._render_timeline(messages, interactions)
        if len(rendered) > self._max_input_chars:
            raise AssistantContextTooLarge(
                "assistant conversation context exceeds configured character limit"
            )
        return GroupAssistantContext(
            rendered=rendered,
            message_count=len(messages),
            assistant_turn_count=len(interactions),
            input_chars=len(rendered),
        )

    def _render_timeline(
        self,
        messages: tuple[InternalMessage, ...],
        interactions: tuple[StoredAssistantInteraction, ...],
    ) -> str:
        entries: list[tuple[datetime, int, int, str]] = []
        for index, message in enumerate(messages):
            assert message.timestamp is not None
            entries.append(
                (
                    message.timestamp,
                    0,
                    message.source_raw_event_id or index,
                    self._renderer.render_message(message),
                )
            )
        for stored in interactions:
            interaction = stored.interaction
            timestamp = interaction.trigger_timestamp
            entries.append(
                (
                    timestamp,
                    1,
                    stored.id,
                    f"[{timestamp.isoformat()}] [机器人生成内容，不是群聊事实]: "
                    f"{interaction.result.answer}",
                )
            )
        entries.sort(key=lambda item: (item[0], item[1], item[2]))
        return "\n".join(item[3] for item in entries)
