import copy
from collections.abc import Mapping
from typing import Any

import pytest

from app.adapters.onebot.client import OneBotActionTimeoutError, OneBotDisconnectedError
from app.domain.messages import (
    ForwardResolutionStatus,
    ForwardSegment,
    IdentityAvailability,
    IdentitySource,
    ReplyResolutionStatus,
    ReplySegment,
    TextSegment,
    UnknownSegment,
)
from app.parsers import OneBotMessageParser
from app.services import ReferenceEnrichmentService


def message_event(segments: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": "outer-message",
        "group_id": "group-1",
        "user_id": "outer-user",
        "time": 1_700_000_000,
        "sender": {"nickname": "Outer sender", "card": "Outer card"},
        "message": segments,
    }
    event.update(overrides)
    return event


def parse(event: Mapping[str, Any]):
    message = OneBotMessageParser().parse(event, source_raw_event_id=42)
    assert message is not None
    return message


def ok(data: Mapping[str, Any]) -> dict[str, object]:
    return {"status": "ok", "retcode": 0, "data": dict(data)}


class FakeReferenceClient:
    def __init__(
        self,
        *,
        messages: Mapping[str, object] | None = None,
        forwards: Mapping[str, object] | None = None,
    ) -> None:
        self.messages = dict(messages or {})
        self.forwards = dict(forwards or {})
        self.message_calls: list[str | int] = []
        self.forward_calls: list[str] = []

    async def get_message(self, message_id: str | int) -> dict[str, object]:
        self.message_calls.append(message_id)
        result = self.messages[str(message_id)]
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    async def get_forward_message(self, message_id: str) -> dict[str, object]:
        self.forward_calls.append(message_id)
        result = self.forwards[message_id]
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


def service(client: FakeReferenceClient) -> ReferenceEnrichmentService:
    return ReferenceEnrichmentService(onebot_client=client, parser=OneBotMessageParser())


@pytest.mark.asyncio
async def test_reply_resolution_uses_resolved_message_author_not_outer_actor() -> None:
    original = parse(message_event([{"type": "reply", "data": {"id": "reply-1"}}]))
    client = FakeReferenceClient(
        messages={
            "reply-1": ok(
                {
                    "message_id": "reply-1",
                    "user_id": "referenced-user",
                    "time": 1_699_999_999,
                    "sender": {"nickname": "Referenced sender"},
                    "message": [{"type": "text", "data": {"text": "hidden"}}],
                }
            )
        }
    )

    enriched = await service(client).enrich(original)

    reply = enriched.segments[0]
    assert isinstance(reply, ReplySegment)
    assert enriched is not original
    assert enriched.actor.user_id == "outer-user"
    assert enriched.author.user_id == "outer-user"
    assert reply.resolution_status is ReplyResolutionStatus.RESOLVED
    assert reply.resolved_message is not None
    assert reply.resolved_message.author.user_id == "referenced-user"
    assert reply.resolved_message.author.source is IdentitySource.RESOLVED_MESSAGE
    assert reply.resolved_message.author.user_id != enriched.actor.user_id
    assert isinstance(reply.resolved_message.segments[0], TextSegment)
    assert client.message_calls == ["reply-1"]


@pytest.mark.asyncio
async def test_reply_missing_resolved_sender_stays_unavailable() -> None:
    original = parse(message_event([{"type": "reply", "data": {"id": "reply-missing"}}]))
    client = FakeReferenceClient(
        messages={
            "reply-missing": ok(
                {
                    "message_id": "reply-missing",
                    "message": [{"type": "text", "data": {"text": "hidden"}}],
                }
            )
        }
    )

    enriched = await service(client).enrich(original)

    reply = enriched.segments[0]
    assert isinstance(reply, ReplySegment)
    assert reply.resolved_message is not None
    assert reply.resolved_message.author.user_id is None
    assert reply.resolved_message.author.availability is IdentityAvailability.UNAVAILABLE
    assert reply.resolved_message.author.source is IdentitySource.UNKNOWN
    assert reply.resolved_message.author.user_id != enriched.actor.user_id


@pytest.mark.asyncio
async def test_reply_invalid_and_failed_resolutions_preserve_original_reference() -> None:
    missing = parse(message_event([{"type": "reply", "data": {"extra": "kept"}}]))
    timeout = parse(message_event([{"type": "reply", "data": {"id": "timeout"}}]))
    malformed = parse(message_event([{"type": "reply", "data": {"id": "bad"}}]))
    client = FakeReferenceClient(
        messages={
            "timeout": OneBotActionTimeoutError("timeout"),
            "bad": ok({"message_id": "bad"}),
        }
    )
    enrichment = service(client)

    invalid_result = await enrichment.enrich(missing)
    timeout_result = await enrichment.enrich(timeout)
    malformed_result = await enrichment.enrich(malformed)

    invalid = invalid_result.segments[0]
    timed_out = timeout_result.segments[0]
    invalid_response = malformed_result.segments[0]
    assert isinstance(invalid, ReplySegment)
    assert isinstance(timed_out, ReplySegment)
    assert isinstance(invalid_response, ReplySegment)
    assert invalid.resolution_status is ReplyResolutionStatus.INVALID_REFERENCE
    assert timed_out.resolution_status is ReplyResolutionStatus.FETCH_FAILED
    assert invalid_response.resolution_status is ReplyResolutionStatus.INVALID_RESPONSE
    assert invalid.raw_data == {"extra": "kept"}
    assert timed_out.raw_data == {"id": "timeout"}
    assert invalid_response.raw_data == {"id": "bad"}
    assert client.message_calls == ["timeout", "bad"]


@pytest.mark.asyncio
async def test_duplicate_reply_ids_are_fetched_once_and_input_is_not_changed() -> None:
    original = parse(
        message_event(
            [
                {"type": "reply", "data": {"id": "same", "keep": [1]}},
                {"type": "reply", "data": {"id": "same", "keep": [2]}},
            ]
        )
    )
    original_copy = copy.deepcopy(original)
    client = FakeReferenceClient(
        messages={
            "same": ok(
                {
                    "message_id": "same",
                    "user_id": "reply-author",
                    "message": [{"type": "text", "data": {"text": "hidden"}}],
                }
            )
        }
    )

    enriched = await service(client).enrich(original)

    assert client.message_calls == ["same"]
    assert original == original_copy
    assert all(
        isinstance(segment, ReplySegment)
        and segment.resolution_status is ReplyResolutionStatus.UNRESOLVED
        for segment in original.segments
    )
    assert all(
        isinstance(segment, ReplySegment)
        and segment.resolution_status is ReplyResolutionStatus.RESOLVED
        for segment in enriched.segments
    )


def forward_response(nodes: list[dict[str, object]]) -> dict[str, object]:
    return ok({"messages": nodes})


@pytest.mark.asyncio
async def test_forward_resolution_preserves_tree_order_and_independent_senders() -> None:
    original = parse(message_event([{"type": "forward", "data": {"id": "forward-1"}}]))
    remote_data = {
        "messages": [
            {
                "type": "node",
                "data": {
                    "id": "node-1",
                    "user_id": "node-user",
                    "nickname": "Node sender",
                    "time": 1_699_999_998,
                    "content": [
                        {"type": "text", "data": {"text": "first"}},
                        {"type": "future", "data": {"deep": [1, 2]}},
                    ],
                },
            },
            {
                "type": "node",
                "data": {
                    "id": "node-2",
                    "content": [{"type": "text", "data": {"text": "second"}}],
                },
            },
        ]
    }
    client = FakeReferenceClient(forwards={"forward-1": ok(remote_data)})

    enriched = await service(client).enrich(original)

    forward = enriched.segments[0]
    assert isinstance(forward, ForwardSegment)
    assert forward.resolved is True
    assert forward.resolution_status is ForwardResolutionStatus.FETCHED
    assert [node.sender.user_id for node in forward.nodes] == ["node-user", None]
    assert forward.nodes[0].sender.availability is IdentityAvailability.KNOWN
    assert forward.nodes[1].sender.availability is IdentityAvailability.UNAVAILABLE
    assert forward.nodes[1].sender.user_id != enriched.actor.user_id
    assert [type(segment) for segment in forward.nodes[0].content] == [
        TextSegment,
        UnknownSegment,
    ]
    assert forward.nodes[0].content[1].raw_data == {"deep": [1, 2]}
    assert original.segments[0].raw_data == {"id": "forward-1"}
    assert original.segments[0].resolved_raw_data is None
    assert forward.raw_data == {"id": "forward-1"}
    assert forward.resolved_raw_data == remote_data
    assert client.forward_calls == ["forward-1"]


@pytest.mark.asyncio
async def test_embedded_forward_is_not_fetched_and_nested_unresolved_is_not_network_resolved() -> None:
    embedded = parse(
        message_event(
            [
                {
                    "type": "forward",
                    "data": {
                        "id": "embedded",
                        "content": [
                            {
                                "type": "node",
                                "data": {
                                    "id": "node",
                                    "content": [
                                        {"type": "forward", "data": {"id": "nested"}}
                                    ],
                                },
                            }
                        ],
                    },
                }
            ]
        )
    )
    unresolved = parse(message_event([{"type": "forward", "data": {"id": "outer"}}]))
    client = FakeReferenceClient(
        forwards={
            "outer": forward_response(
                [
                    {
                        "type": "node",
                        "data": {
                            "id": "remote-node",
                            "content": [
                                {"type": "forward", "data": {"id": "nested"}}
                            ],
                        },
                    }
                ]
            )
        }
    )
    enrichment = service(client)

    embedded_result = await enrichment.enrich(embedded)
    remote_result = await enrichment.enrich(unresolved)

    assert isinstance(embedded_result.segments[0], ForwardSegment)
    assert embedded_result.segments[0].resolution_status is ForwardResolutionStatus.EMBEDDED
    remote = remote_result.segments[0]
    assert isinstance(remote, ForwardSegment)
    nested = remote.nodes[0].content[0]
    assert isinstance(nested, ForwardSegment)
    assert nested.resolution_status is ForwardResolutionStatus.UNRESOLVED
    assert client.forward_calls == ["outer"]


@pytest.mark.asyncio
async def test_forward_failures_and_duplicate_ids_are_safe_and_cached_per_call() -> None:
    original = parse(
        message_event(
            [
                {"type": "forward", "data": {"id": "same"}},
                {"type": "forward", "data": {"id": "same"}},
                {"type": "forward", "data": {"id": "disconnected"}},
                {"type": "forward", "data": {"id": "invalid"}},
            ]
        )
    )
    client = FakeReferenceClient(
        forwards={
            "same": forward_response([]),
            "disconnected": OneBotDisconnectedError("gone"),
            "invalid": ok({"not_messages": []}),
        }
    )

    enriched = await service(client).enrich(original)

    first, second, disconnected, invalid = enriched.segments
    assert isinstance(first, ForwardSegment)
    assert isinstance(second, ForwardSegment)
    assert isinstance(disconnected, ForwardSegment)
    assert isinstance(invalid, ForwardSegment)
    assert first.resolution_status is ForwardResolutionStatus.FETCHED
    assert second.resolution_status is ForwardResolutionStatus.FETCHED
    assert disconnected.resolution_status is ForwardResolutionStatus.FETCH_FAILED
    assert invalid.resolution_status is ForwardResolutionStatus.INVALID_RESPONSE
    assert first.raw_data == {"id": "same"}
    assert disconnected.raw_data == {"id": "disconnected"}
    assert client.forward_calls == ["same", "disconnected", "invalid"]
