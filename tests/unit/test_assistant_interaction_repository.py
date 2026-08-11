from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.domain.assistant_interactions import (
    AssistantInteraction,
    AssistantResult,
    AssistantTriggerType,
)
from app.domain.messages import (
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
)
from app.storage.assistant_interaction_repository import AssistantInteractionRepository
from app.storage.database import Database


NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def interaction(
    *,
    group_id: str = "group-a",
    trigger_message_id: str = "trigger-1",
    response_message_id: str | None = "response-1",
    timestamp: datetime = NOW,
) -> AssistantInteraction:
    return AssistantInteraction(
        platform="qq_official",
        group_id=group_id,
        trigger_type=AssistantTriggerType.MENTION_CHAT,
        trigger_message_id=trigger_message_id,
        trigger_timestamp=timestamp,
        requester=IdentityRef(
            platform="qq_official",
            user_id="user-a",
            display_name="用户A",
            card=None,
            source=IdentitySource.EVENT,
            availability=IdentityAvailability.KNOWN,
        ),
        question="问题",
        result=AssistantResult(
            answer="回答",
            provider="fake",
            model="fake-model",
            finish_reason="stop",
            input_chars=12,
            prompt_version="test-v1",
            prompt_tokens=2,
            completion_tokens=3,
            total_tokens=5,
        ),
        response_message_id=response_message_id,
    )


@pytest.mark.asyncio
async def test_claim_is_durable_and_duplicate_safe(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'assistant-claim.db'}")
    await database.initialize()
    repository = AssistantInteractionRepository(
        database.session_factory,
        clock=lambda: NOW,
    )
    kwargs = {
        "platform": "qq_official",
        "group_id": "group-a",
        "trigger_message_id": "trigger-1",
        "trigger_type": AssistantTriggerType.GROUNDED_QA,
    }
    try:
        assert await repository.claim_trigger(**kwargs) is True
        assert await repository.claim_trigger(**kwargs) is False
        assert await repository.exists_for_trigger(**kwargs) is True
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_successful_interactions_are_group_scoped_and_round_trip(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'assistant-history.db'}")
    await database.initialize()
    repository = AssistantInteractionRepository(
        database.session_factory,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    try:
        stored_a = await repository.insert_successful_interaction(interaction())
        await repository.insert_successful_interaction(
            interaction(
                group_id="group-b",
                trigger_message_id="trigger-b",
                response_message_id="response-b",
            )
        )

        assert stored_a.interaction.requester.user_id == "user-a"
        assert stored_a.interaction.result.answer == "回答"
        history_a = await repository.list_recent_for_group(
            platform="qq_official",
            group_id="group-a",
            start_time=NOW - timedelta(minutes=1),
            before_time=NOW + timedelta(minutes=1),
            limit=20,
        )
        assert [item.interaction.trigger_message_id for item in history_a] == [
            "trigger-1"
        ]
        found = await repository.find_by_response_message_id(
            platform="qq_official",
            group_id="group-a",
            response_message_id="response-1",
        )
        assert found is not None
        assert found.interaction.trigger_message_id == "trigger-1"
        assert (
            await repository.find_by_response_message_id(
                platform="qq_official",
                group_id="group-b",
                response_message_id="response-1",
            )
            is None
        )
    finally:
        await database.dispose()
