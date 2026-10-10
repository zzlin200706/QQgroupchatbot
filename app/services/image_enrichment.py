"""Immutable, failure-isolated image understanding for message trees."""

from __future__ import annotations

import logging
import asyncio
import hashlib
from collections.abc import Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

from app.domain.messages import (
    ForwardNode,
    ForwardSegment,
    ImageSegment,
    InternalMessage,
    ReplySegment,
    TextSegment,
)
from app.domain.messages.forward_display import (
    explicit_image_url,
    is_forward_display,
    image_resource_key,
    redact_media_urls,
)
from app.domain.messages.segments import MessageSegment
from app.llm.providers.base import VisionProvider
from app.services.image_resources import (
    ImageResourceResolver, ImageTooLargeError, InvalidImageUrlError,
    UnsupportedImageError, prepare_display_image,
)


logger = logging.getLogger(__name__)

IMAGE_UNAVAILABLE_DESCRIPTION = "无法解析"
IMAGE_DESCRIPTION_PROMPT = """请为群聊记录简洁、客观地描述这张图片。

重点保留可能影响对话的信息，包括：
- 图片展示的主要内容；
- 重要可见文字；
- 截图中的错误、代码、图表、界面、文档或梗图信息。

不要编造不可见细节，不要推断发送者身份，不要根据人脸推断私人身份。
只输出图片描述文本。"""

ResourceKey = tuple[tuple[str, str], ...]


@dataclass
class _DisplayBatch:
    # Scoped to one request or standalone batch, never shared across groups.
    by_url: dict[str, str] = field(default_factory=dict)
    successes: dict[ResourceKey, str] = field(default_factory=dict)
    by_content: dict[str, str] = field(default_factory=dict)
    attempts: dict[ResourceKey, int] = field(default_factory=dict)


class ImageEnrichmentService:
    """Attach derived descriptions without mutating stored source messages."""

    def __init__(
        self,
        *,
        enabled: bool,
        resolver: ImageResourceResolver | None = None,
        provider: VisionProvider | None = None,
        max_forward_display_images: int = 10,
    ) -> None:
        if enabled and (resolver is None or provider is None):
            raise ValueError("enabled image enrichment requires resolver and provider")
        self._enabled = enabled
        self._resolver = resolver
        self._provider = provider
        if max_forward_display_images < 1:
            raise ValueError("max_forward_display_images must be positive")
        self._max_forward_display_images = max_forward_display_images
        self._request_batch: ContextVar[tuple[object, str, str, _DisplayBatch] | None] = ContextVar(
            "image_enrichment_request_batch", default=None,
        )

    @contextmanager
    def request_scope(self, *, platform: str, group_id: str):
        """Share quote/history work within one task and group, then discard it."""
        token = self._request_batch.set((asyncio.current_task(), platform, group_id, _DisplayBatch()))
        try:
            yield
        finally:
            self._request_batch.reset(token)

    async def enrich_messages(
        self,
        messages: Sequence[InternalMessage],
    ) -> tuple[InternalMessage, ...]:
        if not self._enabled:
            return tuple(messages)
        scope = self._request_batch.get()
        batches: dict[tuple[str, str | int], _DisplayBatch] = {}
        enriched: list[InternalMessage] = []
        for index, message in enumerate(messages):
            group_id = message.context.group_id
            if (scope is not None and scope[0] is asyncio.current_task()
                    and scope[1:3] == (message.platform, group_id)):
                cache = scope[3]
            else:
                # Even standalone mixed-group calls must never share descriptions.
                key = (message.platform, group_id if group_id is not None else index)
                cache = batches.setdefault(key, _DisplayBatch())
            enriched.append(await self._enrich_message(message, cache))
        return tuple(enriched)

    async def _enrich_message(self, message: InternalMessage, cache: _DisplayBatch) -> InternalMessage:
        segments = await self._enrich_segments(message.segments, cache)
        return message if segments is message.segments else replace(message, segments=segments)

    async def _enrich_segments(
        self,
        segments: tuple[MessageSegment, ...],
        cache: _DisplayBatch,
        *,
        forward_display: bool = False,
        quoted: bool = False,
    ) -> tuple[MessageSegment, ...]:
        enriched: list[MessageSegment] = []
        changed = False
        for segment in segments:
            if isinstance(segment, TextSegment) and (
                forward_display or (quoted and is_forward_display(segment.text or ""))
            ):
                replacement = await self._describe_display(segment, cache)
            else:
                replacement = await self._enrich_segment(segment, cache)
            enriched.append(replacement)
            changed = changed or replacement is not segment
        return tuple(enriched) if changed else segments

    async def _enrich_segment(self, segment: MessageSegment, cache: _DisplayBatch) -> MessageSegment:
        if isinstance(segment, ImageSegment):
            return await self._describe(segment)
        if isinstance(segment, ReplySegment) and segment.resolved_message is not None:
            resolved_segments = await self._enrich_segments(
                segment.resolved_message.segments, cache, quoted=True
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
            content = await self._enrich_segments(segment.content, cache, forward_display=True)
            nodes: list[ForwardNode] = []
            nodes_changed = False
            for node in segment.nodes:
                node_content = await self._enrich_segments(node.content, cache, forward_display=True)
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

    async def _describe_display(self, segment: TextSegment, cache: _DisplayBatch) -> TextSegment:
        lines: list[str] = []
        changed = False
        for line_number, line in enumerate((segment.text or "").splitlines(keepends=True), 1):
            url = explicit_image_url(line)
            if url is None:
                lines.append(line)
                continue
            changed = True
            key = image_resource_key(url)
            if key in cache.successes:
                description = cache.successes[key]
            elif url in cache.by_url:
                description = cache.by_url[url]
            elif key not in cache.attempts and len(cache.attempts) >= self._max_forward_display_images:
                description = "超过本次转发图片识别上限，未下载"
            elif cache.attempts.get(key, 0) >= 2:
                description = "无法解析（同一图片的备用链接尝试已达上限）"
            else:
                cache.attempts[key] = cache.attempts.get(key, 0) + 1
                description, success = await self._describe_display_image(
                    url, cache, position=segment.position, line_number=line_number,
                )
                cache.by_url[url] = description
                if success:
                    cache.successes[key] = description
            # An immutable, prompt-only copy. Original text/raw/tree stay in storage.
            lines.append(redact_media_urls(line.rstrip("\r\n")))
            indent = line[:len(line) - len(line.lstrip(" \t"))]
            lines.append("\n" + indent + "[该附件的图片描述（AI生成）: "
                         + " ".join(redact_media_urls(description).splitlines()) + "]\n")
        return replace(segment, text="".join(lines)) if changed else segment

    async def _describe_display_image(
        self, url: str, cache: _DisplayBatch, *, position: int, line_number: int,
    ) -> tuple[str, bool]:
        assert self._resolver is not None and self._provider is not None
        image = ImageSegment(position=position, raw_data=None, file=None, url=url,
                             summary=None, sub_type=None, file_size=None)
        stage = "download"
        # Safe correlation only: no URL, token, filename or exception body in logs.
        resource_tag = hashlib.sha256(repr(image_resource_key(url)).encode()).hexdigest()[:12]
        try:
            resolved = await self._resolver.resolve(image)
            stage = "validation"
            resolved = await asyncio.to_thread(prepare_display_image, resolved)
            digest = hashlib.sha256(resolved.content_type.encode() + b"\0" + resolved.data).hexdigest()
            if digest in cache.by_content:
                return cache.by_content[digest], True
            stage = "vision"
            result = await self._provider.describe_image(resolved, prompt=IMAGE_DESCRIPTION_PROMPT)
            if not result.description.strip():
                raise ValueError("vision description was empty")
            cache.by_content[digest] = result.description
            logger.info("forward image succeeded resource=%s position=%s line=%s byte_count=%s",
                        resource_tag, position, line_number, len(resolved.data))
            return result.description, True
        except Exception as error:
            if isinstance(error, ImageTooLargeError):
                reason = "图片大小或像素数量超过安全上限"
            elif isinstance(error, InvalidImageUrlError):
                reason = "图片地址未通过安全检查或域名解析失败"
            elif stage == "vision":
                reason = "视觉模型调用失败"
            elif stage == "validation" or isinstance(error, UnsupportedImageError):
                reason = "图片格式或解码校验失败"
            else:
                reason = "图片下载失败，链接可能过期或网络异常"
            from app.observability import event
            event("image.forward_failed", level="warning", stage=stage,
                  resource=resource_tag, position=position, line=line_number,
                  error_type=type(error).__name__)
            return f"无法解析（{reason}）", False

    async def _describe(self, image: ImageSegment) -> ImageSegment:
        if image.description:
            return image
        assert self._resolver is not None
        assert self._provider is not None
        logger.info("image enrichment started position=%s", image.position)
        try:
            resolved = await self._resolver.resolve(image)
        except Exception as error:
            from app.observability import event
            event(
                "image.fetch_failed",
                level="warning",
                position=image.position,
                error_type=type(error).__name__,
            )
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
            from app.observability import event
            event(
                "image.vision_failed",
                level="warning",
                position=image.position,
                error_type=type(error).__name__,
            )
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
