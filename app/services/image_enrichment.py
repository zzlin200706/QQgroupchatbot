"""Immutable, failure-isolated image understanding for message trees."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from app.domain.messages import (
    ForwardNode,
    ForwardSegment,
    ImageSegment,
    InternalMessage,
    ReplySegment,
)
from app.domain.messages.segments import MessageSegment
from app.llm.providers.base import VisionProvider
from app.services.image_resources import ImageResourceResolver


logger = logging.getLogger(__name__)

IMAGE_UNAVAILABLE_DESCRIPTION = "无法解析"
IMAGE_DESCRIPTION_PROMPT = """请为群聊记录简洁、客观地描述这张图片。

重点保留可能影响对话的信息，包括：
- 图片展示的主要内容；
- 重要可见文字；
- 截图中的错误、代码、图表、界面、文档或梗图信息。

不要编造不可见细节，不要推断发送者身份，不要根据人脸推断私人身份。
只输出图片描述文本。"""


class ImageEnrichmentService:
    """Attach derived descriptions without mutating stored source messages."""

    def __init__(
        self,
        *,
        enabled: bool,
        resolver: ImageResourceResolver | None = None,
        provider: VisionProvider | None = None,
    ) -> None:
        if enabled and (resolver is None or provider is None):
            raise ValueError("enabled image enrichment requires resolver and provider")
        self._enabled = enabled
        self._resolver = resolver
        self._provider = provider

    async def enrich_messages(
        self,
        messages: Sequence[InternalMessage],
    ) -> tuple[InternalMessage, ...]:
        if not self._enabled:
            return tuple(messages)
        return tuple([await self._enrich_message(message) for message in messages])

    async def _enrich_message(self, message: InternalMessage) -> InternalMessage:
        segments = await self._enrich_segments(message.segments)
        return message if segments is message.segments else replace(message, segments=segments)

    async def _enrich_segments(
        self,
        segments: tuple[MessageSegment, ...],
    ) -> tuple[MessageSegment, ...]:
        enriched: list[MessageSegment] = []
        changed = False
        for segment in segments:
            replacement = await self._enrich_segment(segment)
            enriched.append(replacement)
            changed = changed or replacement is not segment
        return tuple(enriched) if changed else segments

    async def _enrich_segment(self, segment: MessageSegment) -> MessageSegment:
        if isinstance(segment, ImageSegment):
            return await self._describe(segment)
        if isinstance(segment, ReplySegment) and segment.resolved_message is not None:
            resolved_segments = await self._enrich_segments(
                segment.resolved_message.segments
            )
            if resolved_segments is segment.resolved_message.segments:
                return segment
            return replace(
                segment,
                resolved_message=replace(
                    segment.resolved_message,
                    segments=resolved_segments,
                ),
            )
        if isinstance(segment, ForwardSegment):
            content = await self._enrich_segments(segment.content)
            nodes: list[ForwardNode] = []
            nodes_changed = False
            for node in segment.nodes:
                node_content = await self._enrich_segments(node.content)
                replacement = (
                    node
                    if node_content is node.content
                    else replace(node, content=node_content)
                )
                nodes.append(replacement)
                nodes_changed = nodes_changed or replacement is not node
            if content is segment.content and not nodes_changed:
                return segment
            return replace(segment, content=content, nodes=tuple(nodes))
        return segment

    async def _describe(self, image: ImageSegment) -> ImageSegment:
        if image.description:
            return image
        assert self._resolver is not None
        assert self._provider is not None
        logger.info("image enrichment started position=%s", image.position)
        try:
            resolved = await self._resolver.resolve(image)
        except Exception as error:
            logger.warning(
                "image resolve failed position=%s error_type=%s",
                image.position,
                type(error).__name__,
            )
            return replace(image, description=IMAGE_UNAVAILABLE_DESCRIPTION)
        try:
            description = await self._provider.describe_image(
                resolved,
                prompt=IMAGE_DESCRIPTION_PROMPT,
            )
        except Exception as error:
            logger.warning(
                "vision request failed position=%s error_type=%s",
                image.position,
                type(error).__name__,
            )
            return replace(image, description=IMAGE_UNAVAILABLE_DESCRIPTION)
        logger.info(
            "image enrichment succeeded position=%s byte_count=%s",
            image.position,
            len(resolved.data),
        )
        return replace(image, description=description.description)
