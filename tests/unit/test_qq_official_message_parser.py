"""Parser coverage sourced from Phase D's captured QQ Official samples."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.domain.messages import (
    AtSegment,
    FileSegment,
    ForwardResolutionStatus,
    ForwardSegment,
    ImageSegment,
    ReplySegment,
    TextSegment,
    UnknownSegment,
)
from app.parsers import QQOfficialMessageParser


SAMPLES_DIRECTORY = Path(__file__).parents[2] / "data" / "qq_official_samples"
MANIFEST_PATH = SAMPLES_DIRECTORY / "manifest.json"


def sample(filename: str) -> dict:
    return json.loads((SAMPLES_DIRECTORY / filename).read_text(encoding="utf-8"))


def parse(filename: str):
    message = QQOfficialMessageParser().parse(sample(filename), raw_event_id=42)
    assert message is not None
    return message


def test_parses_captured_direct_text_and_identity() -> None:
    message = parse("001_text.json")

    assert message.platform == "qq_official"
    assert message.platform_message_id == sample("001_text.json")["data"]["id"]
    assert message.context.group_id == sample("001_text.json")["data"]["group_id"]
    assert message.actor.user_id == sample("001_text.json")["data"]["author"]["id"]
    assert message.author.user_id == message.actor.user_id
    assert message.timestamp is not None
    assert message.segments == (
        TextSegment(position=0, text="phase-d-test-001", raw_data="phase-d-test-001"),
    )


@pytest.mark.parametrize("filename", ["002_at_bot.json", "003_at_member.json"])
def test_captured_mentions_are_structured_only_when_the_payload_confirms_the_target(filename: str) -> None:
    event = sample(filename)
    message = QQOfficialMessageParser().parse(event)
    assert message is not None

    at_segment = next(segment for segment in message.segments if isinstance(segment, AtSegment))
    assert at_segment.target == event["data"]["mentions"][0]["id"]
    assert at_segment.is_all is False
    assert at_segment.raw_data["mention"] == event["data"]["mentions"][0]


def test_captured_text_and_image_keep_field_order_and_attachment_payload() -> None:
    event = sample("006_text_image.json")
    message = QQOfficialMessageParser().parse(event)
    assert message is not None

    assert [type(segment) for segment in message.segments] == [TextSegment, ImageSegment]
    image = message.segments[1]
    assert isinstance(image, ImageSegment)
    assert image.file == event["data"]["attachments"][0]["filename"]
    assert image.url == event["data"]["attachments"][0]["url"]
    assert image.file_size == event["data"]["attachments"][0]["size"]
    assert image.raw_data == event["data"]["attachments"][0]


def test_captured_file_does_not_invent_file_id_or_path() -> None:
    event = sample("009_file.json")
    message = QQOfficialMessageParser().parse(event)
    assert message is not None

    segment = message.segments[0]
    assert isinstance(segment, FileSegment)
    assert segment.name == event["data"]["attachments"][0]["filename"]
    assert segment.file_id is None
    assert segment.path is None
    assert segment.raw_data == event["data"]["attachments"][0]


@pytest.mark.parametrize("filename", ["007_reply_text.json", "008_reply_image.json"])
def test_captured_reply_preserves_ref_idx_without_mislabeling_it_as_a_message_id(filename: str) -> None:
    event = sample(filename)
    message = QQOfficialMessageParser().parse(event)
    assert message is not None

    reply = message.segments[0]
    assert isinstance(reply, ReplySegment)
    assert reply.referenced_message_id is None
    assert reply.raw_data["msg_element"] == event["data"]["msg_elements"][0]
    assert reply.raw_data["message_scene"] == event["data"]["message_scene"]
    assert any("ref_msg_idx=" in item for item in event["data"]["message_scene"]["ext"])


@pytest.mark.parametrize(
    "filename",
    [
        "012_merged_forward.json",
        "013_merged_forward_multi_author.json",
        "014_merged_forward_image.json",
        "015_nested_forward_level2.json",
        "016_nested_forward_level3.json",
    ],
)
def test_captured_message_type_102_stays_unresolved_without_fabricating_forward_nodes(filename: str) -> None:
    event = sample(filename)
    message = QQOfficialMessageParser().parse(event)
    assert message is not None

    segment = message.segments[0]
    assert isinstance(segment, ForwardSegment)
    assert segment.resolved is False
    assert segment.resolution_status is ForwardResolutionStatus.UNRESOLVED
    assert segment.nodes == ()
    assert segment.content == ()
    assert segment.raw_data == event["data"]


def test_captured_nested_forward_rendering_cannot_inherit_or_guess_node_senders() -> None:
    event = sample("015_nested_forward_level2.json")
    message = QQOfficialMessageParser().parse(event)
    assert message is not None
    forward = message.segments[0]
    assert isinstance(forward, ForwardSegment)

    assert "[发送者]" in event["data"]["content"]
    assert forward.nodes == ()
    assert forward.raw_data["author"] == event["data"]["author"]


def test_unknown_attachment_from_a_captured_envelope_is_loss_preserving() -> None:
    event = copy.deepcopy(sample("004_image.json"))
    event["data"]["attachments"][0]["content_type"] = "application/x-qq-future"

    message = QQOfficialMessageParser().parse(event)
    assert message is not None
    segment = message.segments[0]
    assert isinstance(segment, UnknownSegment)
    assert segment.original_type == "attachment:application/x-qq-future"
    assert segment.raw_data == event["data"]["attachments"][0]


def test_manifest_captured_samples_parse_without_crashing() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    parser = QQOfficialMessageParser()
    captured = [item for item in manifest if item["captured"]]

    assert captured
    for item in captured:
        message = parser.parse(sample(item["file"]))
        assert message is not None, item["file"]


def test_non_group_message_dispatch_is_not_parsed() -> None:
    event = sample("001_text.json")
    event["gateway"]["t"] = "AT_MESSAGE_CREATE"
    assert QQOfficialMessageParser().parse(event) is None
