from __future__ import annotations

from app.domain.messages import ForwardSegment, ReplySegment
from app.parsers import OneBotMessageParser
from app.storage.message_codec import encode_message


def test_codec_explicitly_encodes_relations_kinds_depth_and_enum_values() -> None:
    event = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 10,
        "user_id": 20,
        "time": 1700000000,
        "message": [
            {"type": "reply", "data": {"id": 9}},
            {
                "type": "forward",
                "data": {
                    "id": "forward-id",
                    "content": [
                        {"type": "text", "data": {"text": "loose content"}},
                        {
                            "type": "node",
                            "data": {
                                "id": "node-id",
                                "content": [{"type": "text", "data": {"text": "inside"}}],
                            },
                        },
                    ],
                },
            },
        ],
    }
    message = OneBotMessageParser().parse(event, source_raw_event_id=1)
    assert message is not None
    assert isinstance(message.segments[0], ReplySegment)
    assert isinstance(message.segments[1], ForwardSegment)

    record = encode_message(
        message,
        parser_name="onebot_message_parser",
        parser_version="1",
    )

    assert record.actor_source == "event"
    assert record.actor_availability == "known"
    assert record.provenance_source == "direct_event"
    assert [(node.node_kind, node.relation, node.position, node.depth) for node in record.nodes] == [
        ("reply", "segments", 0, 0),
        ("forward", "segments", 1, 0),
        ("text", "content", 0, 1),
        ("forward_node", "nodes", 0, 1),
        ("text", "content", 0, 2),
    ]
    forward_record = record.nodes[1]
    assert forward_record.payload_json["resolution_status"] == "embedded"
    assert "content" not in forward_record.payload_json
    assert "nodes" not in forward_record.payload_json


def test_codec_rejects_untraceable_messages() -> None:
    message = OneBotMessageParser().parse(
        {"post_type": "message", "message": []},
        source_raw_event_id=None,
    )
    assert message is not None

    try:
        encode_message(message, parser_name="parser", parser_version="1")
    except ValueError as error:
        assert "source_raw_event_id" in str(error)
    else:
        raise AssertionError("untraceable normalized message was accepted")
