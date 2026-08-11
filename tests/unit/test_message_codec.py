from __future__ import annotations

from datetime import datetime, timezone

from app.domain.messages import (
    ForwardNode,
    ForwardResolutionStatus,
    ForwardSegment,
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
    InternalMessage,
    MessageContext,
    MessageProvenance,
    ProvenanceSource,
    ReplyResolutionStatus,
    ReplySegment,
    ResolvedMessageReference,
    TextSegment,
)
from app.storage.message_codec import encode_message


def identity(
    *,
    user_id: str | None = "member-1",
    display_name: str | None = "测试成员",
    availability: IdentityAvailability = IdentityAvailability.KNOWN,
) -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id=user_id,
        display_name=display_name,
        card=None,
        source=(
            IdentitySource.EVENT
            if availability is IdentityAvailability.KNOWN
            else IdentitySource.UNKNOWN
        ),
        availability=availability,
    )


def test_codec_explicitly_encodes_relations_kinds_depth_and_enum_values() -> None:
    message = InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id="message-1",
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="group-1",
        ),
        actor=identity(),
        author=identity(),
        timestamp=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
        segments=(
            ReplySegment(
                position=0,
                raw_data={"ref_msg_idx": "REFIDX_1"},
                referenced_message_id=None,
                resolution_status=ReplyResolutionStatus.RESOLVED,
                resolved_message=ResolvedMessageReference(
                    platform_message_id="quoted-1",
                    author=identity(user_id="member-2", display_name="被引用成员"),
                    timestamp=datetime(2026, 8, 10, 11, tzinfo=timezone.utc),
                    segments=(TextSegment(position=0, raw_data=None, text="quoted"),),
                    raw_data={"quoted": True},
                ),
                resolved_raw_data={"resolved": True},
            ),
            ForwardSegment(
                position=1,
                raw_data={"forward": True},
                reference_id="forward-1",
                resolved=True,
                resolution_status=ForwardResolutionStatus.EMBEDDED,
                content=(TextSegment(position=0, raw_data=None, text="loose content"),),
                nodes=(
                    ForwardNode(
                        sender=identity(
                            user_id=None,
                            display_name=None,
                            availability=IdentityAvailability.UNAVAILABLE,
                        ),
                        timestamp=datetime(2026, 8, 10, 10, tzinfo=timezone.utc),
                        content=(TextSegment(position=0, raw_data=None, text="inside"),),
                        provenance=MessageProvenance(
                            source_type=ProvenanceSource.FORWARD_NODE,
                            raw_event_id=1,
                            forward_depth=1,
                        ),
                        raw_data={"node": True},
                    ),
                ),
            ),
        ),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=1,
        ),
    )

    record = encode_message(
        message,
        parser_name="qq_official_message_parser",
        parser_version="1",
    )

    assert record.actor_source == "event"
    assert record.actor_availability == "known"
    assert record.provenance_source == "direct_event"
    assert [(node.node_kind, node.relation, node.position, node.depth) for node in record.nodes] == [
        ("reply", "segments", 0, 0),
        ("resolved_message", "resolved_message", 0, 1),
        ("text", "segments", 0, 2),
        ("forward", "segments", 1, 0),
        ("text", "content", 0, 1),
        ("forward_node", "nodes", 0, 1),
        ("text", "content", 0, 2),
    ]
    reply_record = record.nodes[0]
    forward_record = record.nodes[3]
    assert reply_record.payload_json["resolution_status"] == "resolved"
    assert forward_record.payload_json["resolution_status"] == "embedded"
    assert "content" not in forward_record.payload_json
    assert "nodes" not in forward_record.payload_json


def test_codec_rejects_untraceable_messages() -> None:
    message = InternalMessage(
        platform="qq_official",
        source_raw_event_id=None,
        platform_message_id="message-1",
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="group-1",
        ),
        actor=identity(),
        author=identity(),
        timestamp=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
        segments=(TextSegment(position=0, raw_data=None, text="hello"),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=None,
        ),
    )

    try:
        encode_message(message, parser_name="parser", parser_version="1")
    except ValueError as error:
        assert "source_raw_event_id" in str(error)
    else:
        raise AssertionError("untraceable normalized message was accepted")
