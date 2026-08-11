from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.domain.assistant_interactions import AssistantResult, AssistantTriggerType
from app.domain.messages import (
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
    InternalMessage,
    MessageContext,
    MessageProvenance,
    ProvenanceSource,
    TextSegment,
)
from app.services.group_assistant_handler import (
    ASSISTANT_FAILURE_MESSAGE,
    QA_USAGE_MESSAGE,
    GroupAssistantHandler,
    GroupAssistantStatus,
)


def message(
    text: str,
    *,
    message_id: str = "trigger-1",
    user_id: str = "user-a",
) -> InternalMessage:
    actor = IdentityRef(
        platform="qq_official",
        user_id=user_id,
        display_name=user_id,
        card=None,
        source=IdentitySource.EVENT,
        availability=IdentityAvailability.KNOWN,
    )
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id=message_id,
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="group-a",
        ),
        actor=actor,
        author=actor,
        timestamp=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        segments=(TextSegment(position=0, raw_data=text, text=text),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=1,
        ),
    )


class FakeService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def answer(self, **kwargs: object) -> AssistantResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return AssistantResult(
            answer="assistant answer",
            provider="fake",
            model="fake-model",
            finish_reason="stop",
            input_chars=20,
            prompt_version="test-v1",
            prompt_tokens=2,
            completion_tokens=3,
            total_tokens=5,
        )


class FakeRepository:
    def __init__(self) -> None:
        self.claimed: set[tuple[str, str, str, AssistantTriggerType]] = set()
        self.persisted: list[object] = []

    async def claim_trigger(self, **kwargs: object) -> bool:
        key = (
            str(kwargs["platform"]),
            str(kwargs["group_id"]),
            str(kwargs["trigger_message_id"]),
            kwargs["trigger_type"],
        )
        if key in self.claimed:
            return False
        self.claimed.add(key)  # type: ignore[arg-type]
        return True

    async def insert_successful_interaction(self, interaction: object) -> object:
        self.persisted.append(interaction)
        return interaction


class FakeSender:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, str | None]] = []

    async def send_group_message(
        self,
        group_id: str,
        content: str,
        *,
        msg_id: str | None = None,
    ) -> object:
        self.calls.append((group_id, content, msg_id))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(message_id="bot-response-1")


def handler(
    service: FakeService,
    repository: FakeRepository,
    sender: FakeSender,
    *,
    enabled: bool = True,
) -> GroupAssistantHandler:
    return GroupAssistantHandler(
        service=service,
        repository=repository,  # type: ignore[arg-type]
        sender=sender,
        enabled=enabled,
        cooldown_seconds=0,
    )


@pytest.mark.asyncio
async def test_success_sends_before_persisting_bot_turn() -> None:
    service = FakeService()
    repository = FakeRepository()
    sender = FakeSender()

    status = await handler(service, repository, sender).handle(
        message("#问 明天几点开会？")
    )

    assert status is GroupAssistantStatus.SUCCEEDED
    assert len(service.calls) == 1
    assert sender.calls == [("group-a", "assistant answer", "trigger-1")]
    assert len(repository.persisted) == 1
    persisted = repository.persisted[0]
    assert persisted.requester.user_id == "user-a"  # type: ignore[attr-defined]
    assert persisted.response_message_id == "bot-response-1"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_duplicate_trigger_calls_llm_and_sender_once() -> None:
    service = FakeService()
    repository = FakeRepository()
    sender = FakeSender()
    assistant = handler(service, repository, sender)
    trigger = message("#问 问题")

    first = await assistant.handle(trigger)
    second = await assistant.handle(trigger)

    assert first is GroupAssistantStatus.SUCCEEDED
    assert second is GroupAssistantStatus.DUPLICATE
    assert len(service.calls) == 1
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_llm_failure_sends_safe_text_and_does_not_persist() -> None:
    service = FakeService(error=RuntimeError("provider secret detail"))
    repository = FakeRepository()
    sender = FakeSender()

    status = await handler(service, repository, sender).handle(message("#问 问题"))

    assert status is GroupAssistantStatus.GENERATION_FAILED
    assert sender.calls == [("group-a", ASSISTANT_FAILURE_MESSAGE, "trigger-1")]
    assert repository.persisted == []


@pytest.mark.asyncio
async def test_qq_send_failure_does_not_persist_future_context() -> None:
    service = FakeService()
    repository = FakeRepository()
    sender = FakeSender(error=RuntimeError("qq failed"))

    status = await handler(service, repository, sender).handle(message("#问 问题"))

    assert status is GroupAssistantStatus.SEND_FAILED
    assert repository.persisted == []


@pytest.mark.asyncio
async def test_bare_qa_sends_usage_without_llm() -> None:
    service = FakeService()
    repository = FakeRepository()
    sender = FakeSender()

    status = await handler(service, repository, sender).handle(message(" #问 "))

    assert status is GroupAssistantStatus.USAGE_SENT
    assert service.calls == []
    assert sender.calls == [("group-a", QA_USAGE_MESSAGE, "trigger-1")]
    assert repository.persisted == []


@pytest.mark.asyncio
async def test_disabled_or_ordinary_message_never_calls_llm() -> None:
    service = FakeService()
    repository = FakeRepository()
    sender = FakeSender()

    disabled = await handler(
        service,
        repository,
        sender,
        enabled=False,
    ).handle(message("#问 问题"))
    ordinary = await handler(service, repository, sender).handle(message("你好"))

    assert disabled is GroupAssistantStatus.DISABLED
    assert ordinary is GroupAssistantStatus.NOT_TRIGGER
    assert service.calls == []
    assert sender.calls == []


@pytest.mark.asyncio
async def test_cooldown_is_per_requester_not_whole_group() -> None:
    service = FakeService()
    repository = FakeRepository()
    sender = FakeSender()
    assistant = GroupAssistantHandler(
        service=service,
        repository=repository,  # type: ignore[arg-type]
        sender=sender,
        enabled=True,
        cooldown_seconds=10,
        clock=lambda: 100.0,
    )

    first_a = await assistant.handle(
        message("#问 A1", message_id="a-1", user_id="user-a")
    )
    first_b = await assistant.handle(
        message("#问 B1", message_id="b-1", user_id="user-b")
    )
    second_a = await assistant.handle(
        message("#问 A2", message_id="a-2", user_id="user-a")
    )

    assert first_a is GroupAssistantStatus.SUCCEEDED
    assert first_b is GroupAssistantStatus.SUCCEEDED
    assert second_a is GroupAssistantStatus.COOLDOWN
    assert len(service.calls) == 2
