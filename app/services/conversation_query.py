"""Conversation-level retrieval of versioned normalized messages."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.domain.messages.models import InternalMessage
from app.storage.message_repository import MessageRepository


DEFAULT_CONVERSATION_LIMIT = 500
MAX_CONVERSATION_LIMIT = 2000


class ConversationQueryService:
    """Validate a conversation window and return decoded domain messages."""

    def __init__(
        self,
        repository: MessageRepository,
        *,
        max_limit: int = MAX_CONVERSATION_LIMIT,
    ) -> None:
        if max_limit < 1:
            raise ValueError("max_limit must be at least one")
        self._repository = repository
        self._max_limit = max_limit

    async def get_messages(
        self,
        *,
        platform: str,
        group_id: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = DEFAULT_CONVERSATION_LIMIT,
        parser_name: str | None = None,
        parser_version: str | None = None,
    ) -> Sequence[InternalMessage]:
        """Return ``[start_time, end_time)`` in deterministic send-time order."""

        _require_aware("start_time", start_time)
        _require_aware("end_time", end_time)
        if start_time >= end_time:
            raise ValueError("start_time must be earlier than end_time")
        if limit < 1 or limit > self._max_limit:
            raise ValueError(f"limit must be between 1 and {self._max_limit}")
        if parser_version is not None and parser_name is None:
            raise ValueError("parser_version requires parser_name")
        if not platform:
            raise ValueError("platform must not be empty")
        if not group_id:
            raise ValueError("group_id must not be empty")

        return await self._repository.list_conversation(
            platform=platform,
            group_id=group_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            parser_name=parser_name,
            parser_version=parser_version,
        )


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
