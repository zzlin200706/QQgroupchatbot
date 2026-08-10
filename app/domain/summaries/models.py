"""Business-level summary output independent of any LLM response schema."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SummaryActionItem:
    description: str
    owner: str | None
    deadline: str | None


@dataclass(frozen=True)
class SummaryResult:
    platform: str
    group_id: str
    start_time: datetime
    end_time: datetime
    message_count: int
    summary: str
    topics: tuple[str, ...]
    key_points: tuple[str, ...]
    decisions: tuple[str, ...]
    action_items: tuple[SummaryActionItem, ...]
    open_questions: tuple[str, ...]
    provider: str
    model: str
    finish_reason: str | None
    input_chars: int
    prompt_version: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class StoredSummary:
    """A persisted summary plus storage-generated identity and timestamp."""

    id: int
    created_at: datetime
    result: SummaryResult
