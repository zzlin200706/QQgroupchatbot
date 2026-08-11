"""Provider-neutral domain values for group assistant interactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.domain.messages import IdentityRef


class AssistantMode(str, Enum):
    GROUNDED_QA = "grounded_qa"
    CHAT = "chat"


class AssistantTriggerType(str, Enum):
    GROUNDED_QA = "grounded_qa"
    MENTION_CHAT = "mention_chat"


@dataclass(frozen=True)
class AssistantResult:
    answer: str
    provider: str
    model: str
    finish_reason: str | None
    input_chars: int
    prompt_version: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class AssistantInteraction:
    platform: str
    group_id: str
    trigger_type: AssistantTriggerType
    trigger_message_id: str
    trigger_timestamp: datetime
    requester: IdentityRef
    question: str
    result: AssistantResult
    response_message_id: str | None


@dataclass(frozen=True)
class StoredAssistantInteraction:
    id: int
    created_at: datetime
    interaction: AssistantInteraction
