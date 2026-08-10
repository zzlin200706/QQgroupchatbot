"""Explicit codec between validated summary results and summary ORM rows."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from app.domain.summaries import StoredSummary, SummaryActionItem, SummaryResult
from app.storage.models import SummaryRecord


def encode_summary(result: SummaryResult, *, created_at: datetime) -> SummaryRecord:
    """Create one new ORM row without storing prompts or rendered input."""

    return SummaryRecord(
        platform=result.platform,
        group_id=result.group_id,
        start_time=result.start_time,
        end_time=result.end_time,
        message_count=result.message_count,
        created_at=created_at,
        summary=result.summary,
        topics=list(result.topics),
        key_points=list(result.key_points),
        decisions=list(result.decisions),
        action_items=[
            {
                "description": item.description,
                "owner": item.owner,
                "deadline": item.deadline,
            }
            for item in result.action_items
        ],
        open_questions=list(result.open_questions),
        provider=result.provider,
        model=result.model,
        finish_reason=result.finish_reason,
        input_chars=result.input_chars,
        prompt_version=result.prompt_version,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
    )


def decode_summary(record: SummaryRecord) -> StoredSummary:
    """Rebuild the complete immutable result and storage wrapper."""

    if record.id is None:
        raise ValueError("summary record must be persisted before decoding")
    return StoredSummary(
        id=record.id,
        created_at=record.created_at,
        result=SummaryResult(
            platform=record.platform,
            group_id=record.group_id,
            start_time=record.start_time,
            end_time=record.end_time,
            message_count=record.message_count,
            summary=record.summary,
            topics=_string_tuple(record.topics, "topics"),
            key_points=_string_tuple(record.key_points, "key_points"),
            decisions=_string_tuple(record.decisions, "decisions"),
            action_items=_action_items(record.action_items),
            open_questions=_string_tuple(record.open_questions, "open_questions"),
            provider=record.provider,
            model=record.model,
            finish_reason=record.finish_reason,
            input_chars=record.input_chars,
            prompt_version=record.prompt_version,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            total_tokens=record.total_tokens,
        ),
    )


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"persisted {field} must be a JSON array of strings")
    return tuple(value)


def _action_items(value: object) -> tuple[SummaryActionItem, ...]:
    if not isinstance(value, list):
        raise ValueError("persisted action_items must be a JSON array")
    return tuple(_action_item(item) for item in value)


def _action_item(value: object) -> SummaryActionItem:
    if not isinstance(value, Mapping) or set(value) != {
        "description",
        "owner",
        "deadline",
    }:
        raise ValueError("persisted action item has an invalid shape")
    description = value["description"]
    owner = value["owner"]
    deadline = value["deadline"]
    if not isinstance(description, str):
        raise ValueError("persisted action item description must be a string")
    if owner is not None and not isinstance(owner, str):
        raise ValueError("persisted action item owner must be a string or null")
    if deadline is not None and not isinstance(deadline, str):
        raise ValueError("persisted action item deadline must be a string or null")
    return SummaryActionItem(
        description=description,
        owner=owner,
        deadline=deadline,
    )
