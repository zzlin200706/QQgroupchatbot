import copy
import json
from pathlib import Path

from app.domain.messages import (
    AtSegment,
    FileSegment,
    ForwardResolutionStatus,
    ForwardSegment,
    IdentityAvailability,
    IdentitySource,
    ImageSegment,
    ProvenanceSource,
    ReplySegment,
    TextSegment,
    UnknownSegment,
)
from app.parsers import OneBotMessageParser


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "onebot"
    / "real_group_text_sanitized.json"
)


def parse(event: dict, source_raw_event_id: int = 42):
    message = OneBotMessageParser().parse(
        event,
        source_raw_event_id=source_raw_event_id,
    )
    assert message is not None
    return message


def message_event(segments: list[dict], **overrides: object) -> dict:
    event: dict = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 3001,
        "group_id": 2001,
        "user_id": 1001,
        "time": 1700000000,
        "sender": {"user_id": 1001, "nickname": "Sender", "card": "Group card"},
        "message": segments,
    }
    event.update(overrides)
    return event


def test_parses_direct_group_text_with_distinct_actor_and_author_roles() -> None:
    event = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    message = parse(event, source_raw_event_id=901)

    assert message.source_raw_event_id == 901
    assert message.platform_message_id == "300000001"
    assert message.context.message_type == "group"
    assert message.actor.user_id == "100000001"
    assert message.actor.availability is IdentityAvailability.KNOWN
    assert message.actor.source is IdentitySource.EVENT
    assert message.author.user_id == "100000001"
    assert message.author.source is IdentitySource.EVENT
    assert message.actor is not message.author
    assert message.provenance.source_type is ProvenanceSource.DIRECT_EVENT
    assert message.segments == (
        TextSegment(
            position=0,
            text="[REDACTED]",
            raw_data={"text": "[REDACTED]"},
        ),
    )


def test_preserves_multi_segment_order_and_structure() -> None:
    event = message_event(
        [
            {"type": "text", "data": {"text": "first"}},
            {"type": "at", "data": {"qq": "1002"}},
            {"type": "text", "data": {"text": "second"}},
            {
                "type": "image",
                "data": {
                    "file": "image-file",
                    "url": "https://example.invalid/image",
                    "summary": "image summary",
                    "sub_type": 0,
                    "file_size": 123,
                    "future_image_field": {"kept": True},
                },
            },
        ]
    )

    message = parse(event)

    assert [type(segment) for segment in message.segments] == [
        TextSegment,
        AtSegment,
        TextSegment,
        ImageSegment,
    ]
    assert message.segments[1].position == 1
    assert message.segments[1].target == "1002"
    assert message.segments[3].position == 3
    assert message.segments[3].file_size == 123
    assert message.segments[3].raw_data["future_image_field"] == {"kept": True}


def test_at_all_is_kept_as_a_structured_segment() -> None:
    message = parse(message_event([{"type": "at", "data": {"qq": "all"}}]))

    segment = message.segments[0]
    assert isinstance(segment, AtSegment)
    assert segment.target == "all"
    assert segment.is_all is True


def test_unknown_segment_keeps_original_type_and_raw_data() -> None:
    raw_data = {"foo": {"bar": [1, 2, 3]}}
    message = parse(message_event([{"type": "future_segment", "data": raw_data}]))

    segment = message.segments[0]
    assert isinstance(segment, UnknownSegment)
    assert segment.original_type == "future_segment"
    assert segment.raw_data == raw_data


def test_missing_identity_fields_are_explicitly_unavailable() -> None:
    message = parse(
        {
            "post_type": "message",
            "message": [{"type": "text", "data": {"text": "still parse"}}],
        }
    )

    assert message.context.message_type is None
    assert message.actor.user_id is None
    assert message.actor.availability is IdentityAvailability.UNAVAILABLE
    assert message.author.user_id is None
    assert message.author.availability is IdentityAvailability.UNAVAILABLE


def test_reply_keeps_only_the_explicit_referenced_message_id() -> None:
    message = parse(message_event([{"type": "reply", "data": {"id": 789, "extra": "kept"}}]))

    segment = message.segments[0]
    assert isinstance(segment, ReplySegment)
    assert segment.referenced_message_id == "789"
    assert segment.raw_data == {"id": 789, "extra": "kept"}
    assert not hasattr(segment, "referenced_author")


def test_image_and_file_keep_known_fields_and_complete_raw_data() -> None:
    image_data = {"file": "image", "url": "https://example.invalid/i", "file_size": 2, "x": 1}
    file_data = {
        "file": "report.pdf",
        "name": "Report",
        "file_id": "file-id",
        "file_size": 10,
        "url": "https://example.invalid/f",
        "path": "/unavailable/example",
        "future": {"nested": True},
    }
    message = parse(
        message_event(
            [
                {"type": "image", "data": image_data},
                {"type": "file", "data": file_data},
            ]
        )
    )

    image, file = message.segments
    assert isinstance(image, ImageSegment)
    assert image.file == "image"
    assert image.raw_data == image_data
    assert isinstance(file, FileSegment)
    assert file.file_id == "file-id"
    assert file.raw_data == file_data


def test_forward_reference_stays_unresolved_without_network_access() -> None:
    message = parse(message_event([{"type": "forward", "data": {"id": "forward-1"}}]))

    segment = message.segments[0]
    assert isinstance(segment, ForwardSegment)
    assert segment.reference_id == "forward-1"
    assert segment.resolved is False
    assert segment.resolution_status is ForwardResolutionStatus.UNRESOLVED
    assert segment.nodes == ()


def test_embedded_nested_forward_tree_is_recursive_and_does_not_inherit_sender() -> None:
    event = message_event(
        [
            {
                "type": "forward",
                "data": {
                    "id": "outer-forward",
                    "content": [
                        {
                            "type": "node",
                            "data": {
                                "id": "outer-node",
                                "content": [
                                    {
                                        "type": "forward",
                                        "data": {
                                            "id": "nested-forward",
                                            "content": [
                                                {
                                                    "type": "node",
                                                    "data": {
                                                        "id": "nested-node",
                                                        "user_id": "3003",
                                                        "nickname": "Nested sender",
                                                        "content": [
                                                            {"type": "text", "data": {"text": "inside"}}
                                                        ],
                                                    },
                                                }
                                            ],
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                },
            }
        ]
    )

    message = parse(event)

    outer = message.segments[0]
    assert isinstance(outer, ForwardSegment)
    assert outer.resolved is True
    assert len(outer.nodes) == 1
    outer_node = outer.nodes[0]
    assert outer_node.sender.user_id is None
    assert outer_node.sender.availability is IdentityAvailability.UNAVAILABLE
    assert outer_node.sender.user_id != message.actor.user_id
    assert outer_node.provenance.source_type is ProvenanceSource.FORWARD_NODE

    nested = outer_node.content[0]
    assert isinstance(nested, ForwardSegment)
    nested_node = nested.nodes[0]
    assert nested_node.sender.user_id == "3003"
    assert nested_node.provenance.source_type is ProvenanceSource.NESTED_FORWARD_NODE
    assert isinstance(nested_node.content[0], TextSegment)


def test_forward_depth_limit_preserves_raw_data_as_unresolved() -> None:
    event = message_event(
        [
            {
                "type": "forward",
                "data": {
                    "id": "outer",
                    "content": [
                        {
                            "type": "node",
                            "data": {
                                "content": [{"type": "forward", "data": {"id": "nested", "content": []}}]
                            },
                        }
                    ],
                },
            }
        ]
    )

    message = OneBotMessageParser(max_forward_depth=1).parse(event, source_raw_event_id=42)
    assert message is not None
    outer_node = message.segments[0].nodes[0]
    nested = outer_node.content[0]
    assert isinstance(nested, ForwardSegment)
    assert nested.resolution_status is ForwardResolutionStatus.DEPTH_LIMIT


def test_non_message_event_returns_none() -> None:
    assert OneBotMessageParser().parse(
        {"post_type": "notice", "notice_type": "group_upload"},
        source_raw_event_id=42,
    ) is None


def test_parser_does_not_modify_raw_payload() -> None:
    event = message_event(
        [
            {"type": "text", "data": {"text": "immutable"}},
            {"type": "future", "data": {"nested": [1, {"two": 2}]}},
        ]
    )
    original = copy.deepcopy(event)

    parse(event)

    assert event == original
