"""Codec for successful assistant interactions and their identity snapshots."""

from __future__ import annotations

from datetime import datetime

from app.domain.assistant_interactions import (
    AssistantInteraction,
    AssistantResult,
    AssistantTriggerType,
    StoredAssistantInteraction,
)
from app.domain.messages import IdentityAvailability, IdentityRef, IdentitySource
from app.storage.models import AssistantInteractionRecord


def encode_assistant_interaction(
    interaction: AssistantInteraction,
    *,
    created_at: datetime,
) -> AssistantInteractionRecord:
    requester = interaction.requester
    result = interaction.result
    return AssistantInteractionRecord(
        platform=interaction.platform,
        group_id=interaction.group_id,
        trigger_type=interaction.trigger_type.value,
        trigger_message_id=interaction.trigger_message_id,
        trigger_timestamp=interaction.trigger_timestamp,
        requester_user_id=requester.user_id,
        requester_display_name=requester.display_name,
        requester_card=requester.card,
        requester_source=requester.source.value,
        requester_availability=requester.availability.value,
        question=interaction.question,
        answer=result.answer,
        provider=result.provider,
        model=result.model,
        finish_reason=result.finish_reason,
        prompt_version=result.prompt_version,
        input_chars=result.input_chars,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        response_message_id=interaction.response_message_id,
        created_at=created_at,
    )


def decode_assistant_interaction(
    record: AssistantInteractionRecord,
) -> StoredAssistantInteraction:
    if record.id is None:
        raise ValueError("assistant interaction must be persisted before decoding")
    requester = IdentityRef(
        platform=record.platform,
        user_id=record.requester_user_id,
        display_name=record.requester_display_name,
        card=record.requester_card,
        source=IdentitySource(record.requester_source),
        availability=IdentityAvailability(record.requester_availability),
    )
    result = AssistantResult(
        answer=record.answer,
        provider=record.provider,
        model=record.model,
        finish_reason=record.finish_reason,
        input_chars=record.input_chars,
        prompt_version=record.prompt_version,
        prompt_tokens=record.prompt_tokens,
        completion_tokens=record.completion_tokens,
        total_tokens=record.total_tokens,
    )
    return StoredAssistantInteraction(
        id=record.id,
        created_at=record.created_at,
        interaction=AssistantInteraction(
            platform=record.platform,
            group_id=record.group_id,
            trigger_type=AssistantTriggerType(record.trigger_type),
            trigger_message_id=record.trigger_message_id,
            trigger_timestamp=record.trigger_timestamp,
            requester=requester,
            question=record.question,
            result=result,
            response_message_id=record.response_message_id,
        ),
    )
