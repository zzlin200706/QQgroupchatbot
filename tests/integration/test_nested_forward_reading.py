"""Nested forward readability through raw storage, parsing, vision and assistant."""
import json
import re
from html import escape
from pathlib import Path

import pytest

from app.domain.messages import ForwardSegment, IdentityAvailability, ReplySegment
from app.rendering import MessageRenderer
from app.services.image_enrichment import ImageEnrichmentService
from tests.integration.test_qq_official_assistant_pipeline import (
    build_pipeline, dispatch, forward_dispatch, process,
)
from tests.unit.test_forward_display_images import URL, Resolver, Vision, line


FIXTURES = Path(__file__).parents[1] / "fixtures/qq_official/captured"


@pytest.mark.parametrize("fixture", ["015_nested_forward_level2.json", "016_nested_forward_level3.json"])
async def test_captured_nested_display_text_survives_storage_and_prompt(tmp_path, fixture):
    captured = json.loads((FIXTURES / fixture).read_text())["data"]["content"]
    resolver, vision = Resolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision)
    pipeline = await build_pipeline(tmp_path, ["已阅读内层记录。"], image_enrichment_service=service)
    try:
        await process(pipeline, forward_dispatch(content=captured))
        raw = (await pipeline.raw_repository.list_recent())[0]
        stored = await pipeline.message_repository.get_by_raw_event_id(raw.id)
        assert stored.segments[0].content[0].text == captured
        assert stored.segments[0].nodes == ()  # Display labels are not verified identities.
        assert not resolver.calls
        await process(pipeline, dispatch(group_id="group-a", message_id="nested-question",
            content="#问 请阅读所有内层转发中的文字消息", timestamp="2026-08-11T12:01:00+08:00"))
        prompt = pipeline.provider.requests[0].user_prompt
        for text in re.findall(r"\[消息内容\] ([^\n]+)", captured):
            # Renderer only normalizes CR; every supplied body must remain present.
            for body_line in text.splitlines():
                if body_line.strip():
                    assert escape(body_line.strip(), quote=False) in prompt
        assert "[关联消息]" in prompt and "原作者与层级未核验" in prompt
        assert "media.example.test" not in prompt
        # Captured URLs are sanitized; never silently download them or treat videos as images.
        assert not resolver.calls
    finally:
        await pipeline.worker.stop()
        await pipeline.database.dispose()


async def test_captured_level2_image_lines_with_mock_urls_reach_vision_at_original_depth(tmp_path):
    captured = json.loads((FIXTURES / "015_nested_forward_level2.json").read_text())["data"]["content"]
    counter = iter(range(3))
    # Only replace sanitized media URLs with synthetic allowlisted endpoints.
    content = re.sub(r"https://media.example.test/redacted", lambda _: URL.replace("fixture", f"nested-{next(counter)}"), captured)
    resolver, vision = Resolver(), Vision()
    pipeline = await build_pipeline(tmp_path, ["已阅读内层图片。"], image_enrichment_service=
        ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision))
    try:
        await process(pipeline, forward_dispatch(content=content))
        await process(pipeline, dispatch(group_id="group-a", message_id="nested-images",
            content="内层转发的图片画了什么？", timestamp="2026-08-11T12:01:00+08:00",
            event_type="GROUP_AT_MESSAGE_CREATE"))
        assert len(resolver.calls) == 3
        assert len(vision.calls) == 1  # Mock returns identical image bytes for all URLs.
        prompt = pipeline.provider.requests[0].user_prompt
        assert prompt.count("一张测试截图") == 3
        assert "脱敏测试正文" in prompt and "test-secret" not in prompt
        raw = next(r for r in await pipeline.raw_repository.list_recent() if r.message_id == "forward-history")
        stored = await pipeline.message_repository.get_by_raw_event_id(raw.id)
        assert stored.segments[0].content[0].text == content
    finally:
        await pipeline.worker.stop()
        await pipeline.database.dispose()


async def test_reply_reply_forward_forward_preserves_deep_text_image_and_author_boundaries(tmp_path):
    resolver, vision = Resolver(), Vision()
    pipeline = await build_pipeline(tmp_path, ["最深层约定在14点，图片是测试截图。"], image_enrichment_service=
        ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision))
    try:
        inner = {"message_type": 102, "content": "[群聊的聊天记录]\n        " + line(),
                 "msg_elements": [{"content": "最深层约定在14点"}]}
        event = forward_dispatch(msg_elements=[{"message_type": 103, "content": "外层说明",
            "msg_elements": [{"message_type": 103, "msg_elements": [
                {"message_type": 102, "msg_elements": [inner]},
            ]}]}])
        await process(pipeline, event)
        raw = (await pipeline.raw_repository.list_recent())[0]
        stored = await pipeline.message_repository.get_by_raw_event_id(raw.id)
        assert isinstance(stored.segments[0], ForwardSegment)
        reply = stored.segments[0].nodes[0].content[0]
        assert isinstance(reply, ReplySegment)
        assert reply.resolved_message.author.availability is IdentityAvailability.UNAVAILABLE
        assert "最深层约定在14点" in MessageRenderer().render_message(stored)
        assert raw.raw_payload["d"] == event.data
        await process(pipeline, dispatch(group_id="group-a", message_id="deep-question",
            content="请描述最深层文字和图片。", timestamp="2026-08-11T12:01:00+08:00",
            event_type="GROUP_AT_MESSAGE_CREATE"))
        prompt = pipeline.provider.requests[0].user_prompt
        assert "最深层约定在14点" in prompt and "一张测试截图" in prompt
        assert "test-secret" not in prompt
        assert len(resolver.calls) == len(vision.calls) == 1
        assert "一张测试截图" not in str(stored.segments)
    finally:
        await pipeline.worker.stop()
        await pipeline.database.dispose()
