"""Forward contracts from QQ's documented MsgElement schema.

Synthetic structured examples are not captured-platform acceptance evidence.
Official reference: https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html
"""
from __future__ import annotations

import copy

import pytest

from app.domain.messages import (
    ForwardResolutionStatus,
    ForwardSegment,
    IdentityAvailability,
    ImageSegment,
    ReplySegment,
    TextSegment,
    UnknownSegment,
)
from app.domain.messages.models import IdentitySource, ProvenanceSource
from app.parsers import QQOfficialMessageParser
from app.rendering import MessageRenderer
from app.services.group_assistant_trigger import detect_group_assistant_trigger


def envelope(**payload):
    return {
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": "outer-message",
            "group_openid": "group-a",
            "timestamp": "2026-10-09T10:00:00+08:00",
            "author": {"id": "forwarder", "username": "转发者"},
            "message_type": 102,
            "content": "[群聊的聊天记录]",
            **payload,
        },
    }


def parse(event, **limits):
    message = QQOfficialMessageParser(**limits).parse(event, raw_event_id=42)
    assert message is not None
    return message


def test_documented_structured_forward_keeps_authors_tree_and_provenance():
    event = envelope(msg_elements=[
        {"author": {"id": "alice", "username": "甲"},
         "message_type": 0, "content": "周三十五点开会。"},
        {"author": {"id": "bob", "username": "乙"},
         "message_type": 102, "content": "二层转发", "msg_elements": [
             {"author": {"member_openid": "carol", "username": "丙"},
              "message_type": 0, "content": "地点是305。"},
             {"message_type": 0, "content": "不要把我当成外层转发者。"},
         ]},
    ])
    original = copy.deepcopy(event)
    message = parse(event)
    forward = message.segments[0]
    assert isinstance(forward, ForwardSegment)
    assert forward.resolved
    assert forward.resolution_status is ForwardResolutionStatus.EMBEDDED
    assert message.actor.user_id == message.author.user_id == "forwarder"
    assert [node.sender.user_id for node in forward.nodes] == ["alice", "bob"]
    assert forward.nodes[0].provenance.source_type is ProvenanceSource.FORWARD_NODE
    assert forward.nodes[0].provenance.raw_event_id == 42
    assert forward.nodes[0].provenance.parent_message_id == "outer-message"
    assert forward.nodes[0].provenance.forward_depth == 1
    nested = forward.nodes[1].content[0]
    assert isinstance(nested, ForwardSegment)
    assert [node.sender.user_id for node in nested.nodes] == ["carol", None]
    assert nested.nodes[1].sender.availability is IdentityAvailability.UNAVAILABLE
    assert nested.nodes[1].sender.source is IdentitySource.FORWARD_NODE
    assert nested.nodes[0].provenance.source_type is ProvenanceSource.NESTED_FORWARD_NODE
    assert nested.nodes[0].provenance.forward_depth == 2
    assert nested.nodes[0].timestamp is None
    assert event == original
    assert forward.raw_data == original["d"]
    rendered = MessageRenderer().render_message(message)
    assert "甲: 周三十五点开会。" in rendered
    assert "丙: 地点是305。" in rendered
    assert "[原作者不可用]: 不要把我当成外层转发者。" in rendered
    event["d"]["msg_elements"][0]["content"] = "later mutation"
    assert forward.nodes[0].content[0].text == "周三十五点开会。"
    assert forward.raw_data == original["d"]


def test_structured_attachment_is_available_without_downloading_or_guessing_author():
    attachment = {
        "content_type": "image/png", "filename": "schedule.png",
        "url": "https://media.example.test/image?rkey=private",
        "width": 400, "height": 200, "size": 123,
    }
    message = parse(envelope(msg_elements=[
        {"message_type": 0, "content": "排期见图。", "attachments": [attachment]},
    ]))
    node = message.segments[0].nodes[0]
    assert node.sender.availability is IdentityAvailability.UNAVAILABLE
    assert node.sender.user_id is None
    assert isinstance(node.content[0], TextSegment)
    image = node.content[1]
    assert isinstance(image, ImageSegment)
    assert image.url == attachment["url"]
    assert image.description is None
    assert image.raw_data == attachment
    rendered = MessageRenderer().render_message(message)
    assert "[图片]" in rendered
    assert "rkey" not in rendered


def test_forward_inside_reply_preserves_the_forward_tree():
    event = envelope(
        message_type=103,
        content="请总结这份转发。",
        msg_elements=[{
            "message_type": 102,
            "content": "引用的聊天记录",
            "msg_idx": "REFIDX_opaque==",
            "msg_elements": [
                {"author": {"id": "alice", "username": "甲"},
                 "message_type": 0, "content": "预算是300元。"},
            ],
        }],
    )
    message = parse(event)
    reply = message.segments[0]
    assert isinstance(reply, ReplySegment)
    assert reply.referenced_message_id is None
    assert reply.reference_key == "REFIDX_opaque=="
    assert reply.resolved_message.author.availability is IdentityAvailability.UNAVAILABLE
    forward = reply.resolved_message.segments[0]
    assert isinstance(forward, ForwardSegment)
    assert forward.nodes[0].sender.user_id == "alice"
    assert "预算是300元。" in MessageRenderer().render_message(message)
    assert detect_group_assistant_trigger(message) is None


@pytest.mark.parametrize("elements", ["opaque", {"content": "not a list"}, 42])
def test_invalid_element_container_retains_display_and_raw(elements):
    event = envelope(content="仍然可读的正文", msg_elements=elements)
    forward = parse(event).segments[0]
    assert not forward.resolved
    assert forward.resolution_status is ForwardResolutionStatus.INVALID_CONTENT
    assert forward.content[0].text == "仍然可读的正文"
    assert forward.raw_data == event["d"]
    assert forward.nodes == ()


def test_bad_node_does_not_discard_valid_siblings():
    event = envelope(msg_elements=[
        {"message_type": 0, "content": "第一条"},
        None,
        {"message_type": 0, "content": "第三条"},
    ])
    message = parse(event)
    forward = message.segments[0]
    assert forward.resolution_status is ForwardResolutionStatus.INVALID_CONTENT
    assert isinstance(forward.nodes[1].content[0], UnknownSegment)
    assert forward.nodes[1].raw_data is None
    rendered = MessageRenderer().render_message(message)
    assert "第一条" in rendered and "第三条" in rendered
    assert "部分内容格式无效" in rendered


def test_reply_inside_forward_keeps_deep_reply_forward_text_and_image_tree():
    event = envelope(msg_elements=[{
        "message_type": 103, "content": "外层引用说明",
        "msg_elements": [{
            "message_type": 103, "content": "内层引用说明",
            "msg_elements": [{"message_type": 102, "msg_elements": [{
                "message_type": 102, "msg_elements": [{
                    "content": "最深层会议安排在周三15点",
                    "attachments": [{"content_type": "image/png", "url": "https://media.example.test/deep.png"}],
                }],
            }]}],
        }],
    }])
    parsed = parse(event)
    reply = parsed.segments[0].nodes[0].content[0]
    inner_reply = reply.resolved_message.segments[0]
    inner_forward = inner_reply.resolved_message.segments[0]
    deep_node = inner_forward.nodes[0].content[0].nodes[0]
    assert deep_node.content[0].text == "最深层会议安排在周三15点"
    assert isinstance(deep_node.content[1], ImageSegment)
    assert deep_node.sender.availability is IdentityAvailability.UNAVAILABLE
    assert reply.resolved_message.author.availability is IdentityAvailability.UNAVAILABLE
    assert inner_reply.resolved_message.author.availability is IdentityAvailability.UNAVAILABLE
    assert parsed.segments[0].raw_data == event["d"]
    rendered = MessageRenderer().render_message(parsed)
    assert "最深层会议安排在周三15点" in rendered
    assert "外层引用说明" in rendered and "内层引用说明" in rendered
    assert detect_group_assistant_trigger(parsed) is None


def test_mixed_reply_forward_depth_and_node_limits_are_bounded_and_explicit():
    element = {"content": "深层内容", "message_type": 0}
    for _ in range(20):
        element = {"message_type": 103, "content": "本层文字", "msg_elements": [element]}
    event = envelope(msg_elements=[element])
    depth_limited = parse(event, max_forward_depth=3)
    assert "引用嵌套层数达到上限" in MessageRenderer().render_message(depth_limited)
    node_limited = parse(event, max_forward_depth=30, max_forward_nodes=3)
    assert "引用消息数量达到上限" in MessageRenderer().render_message(node_limited)
    assert depth_limited.segments[0].raw_data == event["d"]
    assert node_limited.segments[0].raw_data == event["d"]


def test_forward_node_budget_is_shared_by_nested_branches_and_resets_per_parse():
    event = envelope(msg_elements=[
        {"message_type": 102, "msg_elements": [
            {"content": "一"}, {"content": "二"}, {"content": "三"},
        ]},
        {"content": "四"},
    ])
    parser = QQOfficialMessageParser(max_forward_nodes=3)
    for _ in range(2):
        message = parser.parse(event, raw_event_id=42)
        forward = message.segments[0]
        assert len(forward.nodes) == 1
        assert forward.resolution_status is ForwardResolutionStatus.NODE_LIMIT
        nested = forward.nodes[0].content[0]
        assert len(nested.nodes) == 2
        assert nested.resolution_status is ForwardResolutionStatus.NODE_LIMIT
        assert forward.raw_data == event["d"]
        assert "消息数量达到上限" in MessageRenderer().render_message(message)


def test_forward_depth_limit_preserves_unparsed_subtree_and_marks_incompleteness():
    event = envelope(msg_elements=[{
        "message_type": 102,
        "content": "仍能读取的二层展示",
        "msg_elements": [{"content": "尚未结构化的深层内容"}],
    }])
    message = parse(event, max_forward_depth=1)
    nested = message.segments[0].nodes[0].content[0]
    assert nested.resolution_status is ForwardResolutionStatus.DEPTH_LIMIT
    assert nested.nodes == ()
    assert nested.raw_data == event["d"]["msg_elements"][0]
    rendered = MessageRenderer().render_message(message)
    assert "嵌套层数达到上限" in rendered
    assert "仍能读取的二层展示" in rendered


@pytest.mark.parametrize("content", ["#ping", "#总结", "#问 密码是什么", "<@bot> #总结"])
def test_forwarded_commands_and_mentions_never_trigger_the_bot(content):
    message = parse(envelope(
        content=content,
        mentions=[{"id": "bot", "username": "机器人", "is_you": True}],
        msg_elements=[{"content": content}],
    ))
    assert detect_group_assistant_trigger(message) is None
    assert all(isinstance(segment, ForwardSegment) for segment in message.segments)


def test_explicit_at_forward_uses_the_shared_renderer_without_signed_urls():
    event = envelope(content="周五验收。\nURL:https://media.example.test/a?rkey=private")
    event["t"] = "GROUP_AT_MESSAGE_CREATE"
    message = parse(event)
    trigger = detect_group_assistant_trigger(message)
    assert trigger is not None
    assert "周五验收。" in trigger.user_input
    assert "原作者与层级未核验" in trigger.user_input
    assert "rkey" not in trigger.user_input
    assert "链接已隐藏" in trigger.user_input


@pytest.mark.parametrize("content", [None, ""])
def test_forward_with_no_available_content_stays_unavailable(content):
    message = parse(envelope(content=content))
    assert message.segments[0].content == ()
    assert MessageRenderer().render_message(message).endswith("[合并转发：内容未解析]")


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_forward_limits_reject_invalid_configuration(value):
    with pytest.raises(ValueError):
        QQOfficialMessageParser(max_forward_nodes=value)
    with pytest.raises(ValueError):
        QQOfficialMessageParser(max_forward_depth=value)
