from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.domain.messages import (
    ForwardResolutionStatus,
    ForwardSegment,
    FileSegment,
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
    ImageSegment,
    InternalMessage,
    MessageContext,
    MessageProvenance,
    ProvenanceSource,
    ReplyResolutionStatus,
    ReplySegment,
    ResolvedMessageReference,
    TextSegment,
)
from app.domain.summaries import StoredSummary, SummaryResult
from app.rendering import SummaryMessageFormatter
from app.services.summary_command import (
    SummaryCommandHandler,
    SummaryCommandStatus,
    is_summary_command,
)


COMMAND_TIME = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)


def identity(user_id: str | None = "actor-1") -> IdentityRef:
    return IdentityRef(
        platform="onebot11",
        user_id=user_id,
        display_name="测试用户" if user_id is not None else None,
        card=None,
        source=IdentitySource.EVENT if user_id is not None else IdentitySource.UNKNOWN,
        availability=(
            IdentityAvailability.KNOWN
            if user_id is not None
            else IdentityAvailability.UNKNOWN
        ),
    )


def message(
    *segments: object,
    platform: str = "onebot11",
    message_type: str | None = "group",
    group_id: str | None = "synthetic-group",
    timestamp: datetime | None = COMMAND_TIME,
    actor_id: str | None = "actor-1",
    provenance: ProvenanceSource = ProvenanceSource.DIRECT_EVENT,
) -> InternalMessage:
    actor = identity(actor_id)
    return InternalMessage(
        platform=platform,
        source_raw_event_id=1,
        platform_message_id="message-1",
        context=MessageContext(
            message_type=message_type,
            sub_type=None,
            group_id=group_id,
        ),
        actor=actor,
        author=actor,
        timestamp=timestamp,
        segments=segments or (TextSegment(position=0, raw_data={}, text="#总结"),),  # type: ignore[arg-type]
        provenance=MessageProvenance(
            source_type=provenance,
            raw_event_id=1,
        ),
    )


def result() -> SummaryResult:
    return SummaryResult(
        platform="onebot11",
        group_id="synthetic-group",
        start_time=COMMAND_TIME,
        end_time=COMMAND_TIME,
        message_count=1,
        summary="安全摘要",
        topics=(),
        key_points=(),
        decisions=(),
        action_items=(),
        open_questions=(),
        provider="fake",
        model="fake",
        finish_reason="stop",
        input_chars=10,
        prompt_version="1",
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
    )


class FakeSummaryService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def summarize(self, **kwargs: object) -> SummaryResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return replace(
            result(),
            platform=kwargs["platform"],  # type: ignore[arg-type]
            group_id=kwargs["group_id"],  # type: ignore[arg-type]
            start_time=kwargs["start_time"],  # type: ignore[arg-type]
            end_time=kwargs["end_time"],  # type: ignore[arg-type]
        )


class BlockingSummaryService(FakeSummaryService):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def summarize(self, **kwargs: object) -> SummaryResult:
        self.calls.append(kwargs)
        self.started.set()
        await self.release.wait()
        return result()


class FakeSummaryRepository:
    def __init__(self, *, error: Exception | None = None, events: list[str] | None = None) -> None:
        self.error = error
        self.results: list[SummaryResult] = []
        self.events = events

    async def persist(self, value: SummaryResult) -> StoredSummary:
        if self.events is not None:
            self.events.append("persist")
        if self.error is not None:
            raise self.error
        self.results.append(value)
        return StoredSummary(id=len(self.results), created_at=COMMAND_TIME, result=value)


class FakeSender:
    def __init__(self, *, error: Exception | None = None, events: list[str] | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.events = events

    async def send_group_message(self, group_id: str, text: str) -> object:
        if self.events is not None:
            self.events.append("send")
        self.calls.append((group_id, text))
        if self.error is not None:
            raise self.error
        return object()


def handler(
    *,
    service: FakeSummaryService | None = None,
    repository: FakeSummaryRepository | None = None,
    sender: FakeSender | None = None,
    enabled: bool = True,
    clock=lambda: 100.0,
    cooldown_seconds: int = 60,
) -> tuple[SummaryCommandHandler, FakeSummaryService, FakeSummaryRepository, FakeSender]:
    service = service or FakeSummaryService()
    repository = repository or FakeSummaryRepository()
    sender = sender or FakeSender()
    return (
        SummaryCommandHandler(
            summary_service=service,
            summary_repository=repository,
            formatter=SummaryMessageFormatter(),
            sender=sender,
            enabled=enabled,
            lookback_minutes=120,
            cooldown_seconds=cooldown_seconds,
            clock=clock,
        ),
        service,
        repository,
        sender,
    )


@pytest.mark.parametrize("text", ["#总结", " #总结 ", "\n#总结\t"])
def test_detects_only_exact_trimmed_top_level_text(text: str) -> None:
    assert is_summary_command(message(TextSegment(0, {}, text)))


@pytest.mark.parametrize("text", ["#总结一下", "abc #总结", "#总结 2h", "/summarize"])
def test_rejects_non_exact_text(text: str) -> None:
    assert not is_summary_command(message(TextSegment(0, {}, text)))


def test_nested_reply_forward_and_image_metadata_never_trigger() -> None:
    quoted = ResolvedMessageReference(
        platform_message_id="quoted",
        author=identity("quoted-author"),
        timestamp=COMMAND_TIME,
        segments=(TextSegment(0, {}, "#总结"),),
        raw_data={"raw": "#总结"},
    )
    reply = ReplySegment(
        position=0,
        raw_data={"metadata": "#总结"},
        referenced_message_id="quoted",
        resolution_status=ReplyResolutionStatus.RESOLVED,
        resolved_message=quoted,
    )
    nested_forward = ForwardSegment(
        position=0,
        raw_data={"raw": "#总结"},
        reference_id="nested-forward",
        resolved=True,
        resolution_status=ForwardResolutionStatus.EMBEDDED,
        content=(TextSegment(0, {}, "#总结"),),
        nodes=(),
    )
    forward = ForwardSegment(
        position=0,
        raw_data={"raw": "#总结"},
        reference_id="forward",
        resolved=True,
        resolution_status=ForwardResolutionStatus.EMBEDDED,
        content=(nested_forward,),
        nodes=(),
    )
    image = ImageSegment(
        position=0,
        raw_data={"summary": "#总结"},
        file="#总结",
        url=None,
        summary="#总结",
        sub_type=None,
        file_size=None,
    )
    file = FileSegment(
        position=0,
        raw_data={"name": "#总结"},
        file=None,
        name="#总结",
        file_id=None,
        file_size=None,
        url=None,
        path=None,
    )

    assert not is_summary_command(message(reply, TextSegment(1, {}, "#总结")))
    assert not is_summary_command(message(forward))
    assert not is_summary_command(message(image))
    assert not is_summary_command(message(file))
    assert not is_summary_command(
        message(TextSegment(0, {}, "#总结"), provenance=ProvenanceSource.FORWARD_NODE)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "post_type", "self_id"),
    [
        (message(message_type="private", group_id=None), "message", "bot-1"),
        (message(group_id=None), "message", "bot-1"),
        (message(timestamp=None), "message", "bot-1"),
        (
            message(timestamp=datetime(2026, 8, 10, 12)),
            "message",
            "bot-1",
        ),
        (message(platform="qq_official"), "message", "bot-1"),
        (message(), "message_sent", "bot-1"),
        (message(actor_id="bot-1"), "message", "bot-1"),
    ],
    ids=[
        "private",
        "missing-group",
        "missing-time",
        "naive-time",
        "other-platform",
        "sent",
        "self",
    ],
)
async def test_invalid_context_never_generates(
    candidate: InternalMessage,
    post_type: str,
    self_id: str,
) -> None:
    command_handler, service, repository, sender = handler()

    status = await command_handler.handle(
        candidate,
        post_type=post_type,
        self_id=self_id,
    )

    assert status is SummaryCommandStatus.INVALID_CONTEXT
    assert service.calls == []
    assert repository.results == []
    assert sender.calls == []


@pytest.mark.asyncio
async def test_disabled_command_never_generates() -> None:
    command_handler, service, _, _ = handler(enabled=False)

    status = await command_handler.handle(message(), post_type="message", self_id="bot")

    assert status is SummaryCommandStatus.DISABLED
    assert service.calls == []


@pytest.mark.asyncio
async def test_window_persistence_and_send_order_are_explicit() -> None:
    events: list[str] = []
    service = FakeSummaryService()
    original_summarize = service.summarize

    async def summarize(**kwargs: object) -> SummaryResult:
        events.append("generate")
        return await original_summarize(**kwargs)

    service.summarize = summarize  # type: ignore[method-assign]
    command_handler, service, repository, sender = handler(
        service=service,
        repository=FakeSummaryRepository(events=events),
        sender=FakeSender(events=events),
    )

    status = await command_handler.handle(message(), post_type="message", self_id="bot")

    assert status is SummaryCommandStatus.SUCCEEDED
    assert events == ["generate", "persist", "send"]
    assert service.calls[0]["platform"] == "onebot11"
    assert service.calls[0]["group_id"] == "synthetic-group"
    assert service.calls[0]["start_time"] == COMMAND_TIME.replace(hour=10)
    assert service.calls[0]["end_time"] == COMMAND_TIME
    assert len(repository.results) == 1
    assert sender.calls == [
        ("synthetic-group", "【群聊总结】\n\n安全摘要")
    ]


@pytest.mark.asyncio
async def test_cooldown_is_per_group_and_uses_injected_clock() -> None:
    current = [100.0]
    command_handler, service, _, _ = handler(clock=lambda: current[0])

    first = await command_handler.handle(message(), post_type="message", self_id="bot")
    second = await command_handler.handle(message(), post_type="message", self_id="bot")
    other_group = await command_handler.handle(
        message(group_id="other-group"), post_type="message", self_id="bot"
    )
    current[0] = 160.0
    third = await command_handler.handle(message(), post_type="message", self_id="bot")

    assert first is SummaryCommandStatus.SUCCEEDED
    assert second is SummaryCommandStatus.COOLDOWN
    assert other_group is SummaryCommandStatus.SUCCEEDED
    assert third is SummaryCommandStatus.SUCCEEDED
    assert len(service.calls) == 3


@pytest.mark.asyncio
async def test_concurrent_same_group_triggers_only_one_generation() -> None:
    service = BlockingSummaryService()
    command_handler, _, repository, sender = handler(service=service)

    first_task = asyncio.create_task(
        command_handler.handle(message(), post_type="message", self_id="bot")
    )
    await asyncio.wait_for(service.started.wait(), timeout=1)
    second = await command_handler.handle(message(), post_type="message", self_id="bot")
    service.release.set()
    first = await asyncio.wait_for(first_task, timeout=1)

    assert first is SummaryCommandStatus.SUCCEEDED
    assert second is SummaryCommandStatus.IN_PROGRESS
    assert len(service.calls) == 1
    assert len(repository.results) == 1
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_generation_failure_does_not_persist_or_send() -> None:
    command_handler, service, repository, sender = handler(
        service=FakeSummaryService(error=RuntimeError("synthetic failure"))
    )

    status = await command_handler.handle(message(), post_type="message", self_id="bot")

    assert status is SummaryCommandStatus.GENERATION_FAILED
    assert len(service.calls) == 1
    assert repository.results == []
    assert sender.calls == []


@pytest.mark.asyncio
async def test_persistence_failure_prevents_send() -> None:
    command_handler, service, repository, sender = handler(
        repository=FakeSummaryRepository(error=RuntimeError("synthetic failure"))
    )

    status = await command_handler.handle(message(), post_type="message", self_id="bot")

    assert status is SummaryCommandStatus.PERSISTENCE_FAILED
    assert len(service.calls) == 1
    assert repository.results == []
    assert sender.calls == []


@pytest.mark.asyncio
async def test_send_failure_keeps_persisted_summary_and_does_not_regenerate() -> None:
    command_handler, service, repository, sender = handler(
        sender=FakeSender(error=RuntimeError("synthetic failure"))
    )

    status = await command_handler.handle(message(), post_type="message", self_id="bot")

    assert status is SummaryCommandStatus.SEND_FAILED
    assert len(service.calls) == 1
    assert len(repository.results) == 1
    assert len(sender.calls) == 1
