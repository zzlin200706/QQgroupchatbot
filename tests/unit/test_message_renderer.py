from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.domain.messages import (
    AtSegment,
    FileSegment,
    ForwardNode,
    ForwardResolutionStatus,
    ForwardSegment,
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
    UnknownSegment,
)
from app.parsers import QQOfficialMessageParser
from app.rendering import MessageRenderer


QQ_SAMPLES = Path(__file__).parents[2] / "data" / "qq_official_samples"
NOW = datetime(2026, 8, 10, 12, 13, 14, tzinfo=timezone.utc)


def identity(
    name: str | None,
    *,
    user_id: str | None = None,
    availability: IdentityAvailability = IdentityAvailability.KNOWN,
    platform: str = "qq_official",
) -> IdentityRef:
    return IdentityRef(
        platform=platform,
        user_id=user_id,
        display_name=name,
        card=None,
        source=(
            IdentitySource.EVENT
            if availability is IdentityAvailability.KNOWN
            else IdentitySource.UNKNOWN
        ),
        availability=availability,
    )


def message(
    *segments,
    author: IdentityRef | None = None,
    platform: str = "qq_official",
    timestamp: datetime | None = NOW,
    raw_event_id: int = 1,
) -> InternalMessage:
    author = author or identity("Alice", user_id="1", platform=platform)
    return InternalMessage(
        platform=platform,
        source_raw_event_id=raw_event_id,
        platform_message_id=f"message-{raw_event_id}",
        context=MessageContext(message_type="group", sub_type=None, group_id="group"),
        actor=author,
        author=author,
        timestamp=timestamp,
        segments=tuple(segments),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=raw_event_id,
        ),
    )


def forward_node(
    sender: IdentityRef,
    *segments,
    depth: int = 1,
) -> ForwardNode:
    return ForwardNode(
        sender=sender,
        timestamp=NOW,
        content=tuple(segments),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.FORWARD_NODE,
            raw_event_id=1,
            forward_depth=depth,
        ),
        raw_data={"private_node_payload": "never-render"},
    )


def test_text_and_multiline_text_have_stable_boundaries() -> None:
    renderer = MessageRenderer()
    single = renderer.render_message(
        message(TextSegment(position=0, raw_data={"secret": 1}, text="hello"))
    )
    multiline = renderer.render_conversation(
        (
            message(TextSegment(position=0, raw_data=None, text="第一行\n第二行")),
            message(
                TextSegment(position=0, raw_data=None, text="next"),
                raw_event_id=2,
            ),
        )
    )

    assert single == "[2026-08-10T12:13:14+00:00] Alice: hello"
    prefix = "[2026-08-10T12:13:14+00:00] Alice: "
    assert multiline == f"{prefix}第一行\n{' ' * len(prefix)}第二行\n{prefix}next"


def test_segment_order_and_exact_id_mention_resolution() -> None:
    alice_message = message(
        TextSegment(position=0, raw_data=None, text="hello "),
        AtSegment(position=1, raw_data=None, target="2", is_all=False),
        TextSegment(position=2, raw_data=None, text=" world "),
        ImageSegment(
            position=3,
            raw_data={"url": "https://secret.invalid/image"},
            file="secret-file-id",
            url="https://secret.invalid/image",
            summary="cat",
            sub_type=None,
            file_size=10,
        ),
    )
    bob_message = message(
        TextSegment(position=0, raw_data=None, text="present"),
        author=identity("Bob", user_id="2"),
        raw_event_id=2,
    )

    rendered = MessageRenderer().render_conversation((alice_message, bob_message))

    assert "Alice: hello @Bob world [图片: cat]" in rendered
    assert "https://secret.invalid/image" not in rendered
    assert MessageRenderer().render_message(alice_message).endswith(
        "hello @用户 world [图片: cat]"
    )


def test_all_file_unknown_and_media_fields_are_safe() -> None:
    rendered = MessageRenderer().render_message(
        message(
            AtSegment(position=0, raw_data={"qq": "all"}, target=None, is_all=True),
            ImageSegment(
                position=1,
                raw_data={"gateway": "PRIVATE_GATEWAY_JSON"},
                file="PRIVATE_IMAGE_ID",
                url="https://private.invalid/image",
                summary=None,
                sub_type=None,
                file_size=None,
            ),
            FileSegment(
                position=2,
                raw_data={"authorization": "PRIVATE_AUTH"},
                file="PRIVATE_FILE",
                name="report.pdf",
                file_id="PRIVATE_FILE_ID",
                file_size=None,
                url="https://private.invalid/file",
                path="/private/path",
            ),
            UnknownSegment(
                position=3,
                raw_data={"PRIVATE_RAW": True},
                original_type="future_type",
            ),
            UnknownSegment(position=4, raw_data="PRIVATE_RAW_2", original_type=None),
        )
    )

    assert rendered.endswith(
        "@全体成员[图片][文件: report.pdf][未知消息类型: future_type][未知消息类型]"
    )
    for private_value in (
        "PRIVATE_GATEWAY_JSON",
        "PRIVATE_IMAGE_ID",
        "https://private.invalid/image",
        "PRIVATE_AUTH",
        "PRIVATE_FILE_ID",
        "https://private.invalid/file",
        "/private/path",
        "PRIVATE_RAW",
    ):
        assert private_value not in rendered


def test_unresolved_reply_hides_ids_by_default_and_resolved_reply_uses_quoted_author() -> None:
    unresolved = ReplySegment(
        position=0,
        raw_data={"ref_msg_idx": "REFIDX_PRIVATE"},
        referenced_message_id="platform-reference-1",
        resolution_status=ReplyResolutionStatus.UNRESOLVED,
    )
    no_id = replace(unresolved, referenced_message_id=None)
    resolved = replace(
        unresolved,
        resolution_status=ReplyResolutionStatus.RESOLVED,
        resolved_message=ResolvedMessageReference(
            platform_message_id="platform-reference-1",
            author=identity("Bob", user_id="2"),
            timestamp=NOW,
            segments=(TextSegment(position=0, raw_data=None, text="quoted"),),
            raw_data={"PRIVATE_RESPONSE_JSON": True},
        ),
        resolved_raw_data={"PRIVATE_RESOLVED_RAW": True},
    )

    assert MessageRenderer().render_message(message(unresolved)).endswith(
        "[回复某条消息]"
    )
    assert MessageRenderer(include_platform_ids=True).render_message(
        message(unresolved)
    ).endswith("[回复消息 platform-reference-1]")
    assert MessageRenderer().render_message(message(no_id)).endswith(
        "[回复消息，引用ID不可用]"
    )
    rendered = MessageRenderer().render_message(
        message(resolved, TextSegment(position=1, raw_data=None, text=" current"))
    )
    assert rendered.endswith("[回复 Bob: quoted] current")
    assert "Alice: quoted" not in rendered
    assert "PRIVATE_RESPONSE_JSON" not in rendered
    assert "PRIVATE_RESOLVED_RAW" not in rendered


def test_forward_unresolved_content_and_nodes_remain_distinct() -> None:
    unresolved = ForwardSegment(
        position=0,
        raw_data={"content": "PRIVATE_RENDERED_TEXT"},
        reference_id="PRIVATE_FORWARD_ID",
        resolved=False,
        resolution_status=ForwardResolutionStatus.UNRESOLVED,
        content=(),
        nodes=(),
        resolved_raw_data={"PRIVATE_FORWARD_RESPONSE": True},
    )
    resolved = replace(
        unresolved,
        resolved=True,
        resolution_status=ForwardResolutionStatus.EMBEDDED,
        content=(TextSegment(position=0, raw_data=None, text="loose content"),),
        nodes=(
            forward_node(
                identity("Bob", user_id="2"),
                TextSegment(position=0, raw_data=None, text="hello"),
            ),
            forward_node(
                identity(
                    None,
                    availability=IdentityAvailability.UNAVAILABLE,
                ),
                ImageSegment(
                    position=0,
                    raw_data=None,
                    file=None,
                    url="https://private.invalid/forward-image",
                    summary=None,
                    sub_type=None,
                    file_size=None,
                ),
            ),
        ),
    )

    unresolved_text = MessageRenderer().render_message(message(unresolved))
    resolved_text = MessageRenderer().render_message(message(resolved))

    assert unresolved_text.endswith("[合并转发：内容未解析]")
    assert "PRIVATE_RENDERED_TEXT" not in unresolved_text
    assert "PRIVATE_FORWARD_ID" not in unresolved_text
    assert "内容: loose content" in resolved_text
    assert "- Bob: hello" in resolved_text
    assert "- [原作者不可用]: [图片]" in resolved_text
    assert "Alice: [图片]" not in resolved_text
    assert "https://private.invalid/forward-image" not in resolved_text


def test_nested_forward_keeps_hierarchy_and_unknown_is_not_unavailable() -> None:
    nested = ForwardSegment(
        position=0,
        raw_data=None,
        reference_id="nested",
        resolved=True,
        resolution_status=ForwardResolutionStatus.EMBEDDED,
        content=(),
        nodes=(
            forward_node(
                identity("D", user_id="4"),
                TextSegment(position=0, raw_data=None, text="nested"),
                depth=2,
            ),
            forward_node(
                identity(None, availability=IdentityAvailability.UNAVAILABLE),
                TextSegment(position=0, raw_data=None, text="old"),
                depth=2,
            ),
        ),
    )
    outer = replace(
        nested,
        reference_id="outer",
        nodes=(
            forward_node(
                identity("B", user_id="2"),
                TextSegment(position=0, raw_data=None, text="hello"),
            ),
            forward_node(
                identity(None, availability=IdentityAvailability.UNKNOWN),
                nested,
            ),
        ),
    )

    rendered = MessageRenderer().render_message(message(outer))
    lines = rendered.splitlines()

    assert any("- B: hello" in line for line in lines)
    assert any("- [作者未知]:" in line for line in lines)
    assert any("      [合并转发]" in line for line in lines)
    assert any("        - D: nested" in line for line in lines)
    assert any("        - [原作者不可用]: old" in line for line in lines)


def test_forward_node_preserves_own_author() -> None:
    rendered = MessageRenderer().render_message(
        message(
            ForwardSegment(
                position=0,
                raw_data=None,
                reference_id=None,
                resolved=True,
                resolution_status=ForwardResolutionStatus.EMBEDDED,
                content=(),
                nodes=(
                    forward_node(
                        identity("Bob", user_id="2"),
                        TextSegment(position=0, raw_data=None, text="hello"),
                    ),
                ),
            )
        )
    )

    assert "- Bob: hello" in rendered
    assert "Alice: hello" not in rendered


def test_forward_node_without_author_does_not_inherit_event_sender() -> None:
    rendered = MessageRenderer().render_message(
        message(
            ForwardSegment(
                position=0,
                raw_data=None,
                reference_id=None,
                resolved=True,
                resolution_status=ForwardResolutionStatus.EMBEDDED,
                content=(),
                nodes=(
                    forward_node(
                        identity(None, availability=IdentityAvailability.UNAVAILABLE),
                        TextSegment(position=0, raw_data=None, text="hello"),
                    ),
                ),
            )
        )
    )

    assert "- [原作者不可用]: hello" in rendered
    assert "Alice: hello" not in rendered


def test_nested_forward_node_without_author_does_not_inherit_parent_sender() -> None:
    nested = ForwardSegment(
        position=0,
        raw_data=None,
        reference_id=None,
        resolved=True,
        resolution_status=ForwardResolutionStatus.EMBEDDED,
        content=(),
        nodes=(
            forward_node(
                identity(None, availability=IdentityAvailability.UNAVAILABLE),
                TextSegment(position=0, raw_data=None, text="nested"),
                depth=2,
            ),
        ),
    )
    rendered = MessageRenderer().render_message(
        message(
            ForwardSegment(
                position=0,
                raw_data=None,
                reference_id=None,
                resolved=True,
                resolution_status=ForwardResolutionStatus.EMBEDDED,
                content=(),
                nodes=(
                    forward_node(
                        identity("Bob", user_id="2"),
                        nested,
                    ),
                ),
            )
        )
    )

    assert "- Bob:" in rendered
    assert "        - [原作者不可用]: nested" in rendered
    assert "        - Bob: nested" not in rendered
    assert "Alice: nested" not in rendered


def test_identity_labels_timezone_and_truncation_are_explicit() -> None:
    known_without_name = identity(None, user_id="123")
    unknown = identity(None, availability=IdentityAvailability.UNKNOWN)
    unavailable = identity(None, availability=IdentityAvailability.UNAVAILABLE)

    assert "已知用户:" in MessageRenderer().render_message(
        message(TextSegment(position=0, raw_data=None, text="x"), author=known_without_name)
    )
    assert "[作者未知]:" in MessageRenderer().render_message(
        message(TextSegment(position=0, raw_data=None, text="x"), author=unknown)
    )
    assert "[原作者不可用]:" in MessageRenderer().render_message(
        message(TextSegment(position=0, raw_data=None, text="x"), author=unavailable)
    )
    shifted = MessageRenderer(
        display_timezone=timezone(timedelta(hours=8))
    ).render_message(message(TextSegment(position=0, raw_data=None, text="x")))
    assert shifted.startswith("[2026-08-10T20:13:14+08:00]")
    truncated = MessageRenderer(max_chars=20).render_message(
        message(TextSegment(position=0, raw_data=None, text="x" * 100))
    )
    assert len(truncated) == 20
    assert truncated.endswith("[内容已截断]")
    with pytest.raises(ValueError, match="timezone-aware"):
        MessageRenderer().render_message(
            message(
                TextSegment(position=0, raw_data=None, text="x"),
                timestamp=datetime(2026, 8, 10),
            )
        )


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("001_text.json", "phase-d-test-001"),
        ("002_at_bot.json", "@用户"),
        ("004_image.json", "[图片]"),
        ("009_file.json", "[文件:"),
    ],
)
def test_qq_official_captured_samples_render_safely(filename: str, expected: str) -> None:
    payload = json.loads((QQ_SAMPLES / filename).read_text(encoding="utf-8"))
    parsed = QQOfficialMessageParser().parse(payload, raw_event_id=1)
    assert parsed is not None

    rendered = MessageRenderer().render_message(parsed)

    assert expected in rendered
    for attachment in payload["data"].get("attachments", []):
        if attachment.get("url"):
            assert attachment["url"] not in rendered


def test_qq_official_103_refidx_and_102_forward_are_not_reparsed() -> None:
    reply_payload = json.loads((QQ_SAMPLES / "007_reply_text.json").read_text(encoding="utf-8"))
    forward_payload = json.loads(
        (QQ_SAMPLES / "015_nested_forward_level2.json").read_text(encoding="utf-8")
    )
    reply = QQOfficialMessageParser().parse(reply_payload, raw_event_id=1)
    forward = QQOfficialMessageParser().parse(forward_payload, raw_event_id=2)
    assert reply is not None and forward is not None

    reply_text = MessageRenderer(include_platform_ids=True).render_message(reply)
    forward_text = MessageRenderer().render_message(forward)

    assert "[回复 [原作者不可用]: 原消息后恢复原消息]" in reply_text
    assert "REFIDX_" not in reply_text
    assert forward_text.endswith("[合并转发：内容未解析]")
    assert "[发送者]" not in forward_text
    parsed_forward = forward.segments[0]
    assert isinstance(parsed_forward, ForwardSegment)
    assert parsed_forward.nodes == ()
