"""Pure deterministic text rendering for the lossless message domain tree."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import timezone, tzinfo

from app.domain.messages.models import IdentityAvailability, IdentityRef, InternalMessage
from app.domain.messages.segments import (
    AtSegment,
    FileSegment,
    ForwardNode,
    ForwardSegment,
    ImageSegment,
    MessageSegment,
    ReplySegment,
    ResolvedMessageReference,
    TextSegment,
    UnknownSegment,
)


TRUNCATION_MARKER = "[内容已截断]"
RECURSION_MARKER = "[递归内容已截断]"


class MessageRenderer:
    """Render domain objects without storage, network, or parser access."""

    def __init__(
        self,
        *,
        display_timezone: tzinfo = timezone.utc,
        include_platform_ids: bool = False,
        max_chars: int | None = None,
    ) -> None:
        if max_chars is not None and (
            isinstance(max_chars, bool) or max_chars < len(TRUNCATION_MARKER)
        ):
            raise ValueError(
                f"max_chars must be at least {len(TRUNCATION_MARKER)} or None"
            )
        self._display_timezone = display_timezone
        self._include_platform_ids = include_platform_ids
        self._max_chars = max_chars

    @property
    def max_chars(self) -> int | None:
        """Expose whether this presentation renderer is configured to truncate."""

        return self._max_chars

    def render_message(self, message: InternalMessage) -> str:
        """Render one message, resolving mentions only from that message tree."""

        names = _identity_names((message,))
        return self._truncate(self._render_message(message, names))

    def render_conversation(self, messages: Sequence[InternalMessage]) -> str:
        """Render messages in caller-provided order with exact-ID mention lookup."""

        names = _identity_names(messages)
        rendered = "\n".join(self._render_message(message, names) for message in messages)
        return self._truncate(rendered)

    def _render_message(
        self,
        message: InternalMessage,
        names: dict[tuple[str, str], str],
    ) -> str:
        timestamp = "时间不可用"
        if message.timestamp is not None:
            if message.timestamp.tzinfo is None or message.timestamp.utcoffset() is None:
                raise ValueError("message timestamp must be timezone-aware")
            timestamp = message.timestamp.astimezone(self._display_timezone).isoformat()
        prefix = f"[{timestamp}] {_identity_label(message.author)}: "
        body = self._render_segments(
            message.segments,
            platform=message.platform,
            names=names,
            active=set(),
        )
        if not body:
            body = "[空消息]"
        return prefix + body.replace("\n", "\n" + " " * len(prefix))

    def _render_segments(
        self,
        segments: Sequence[MessageSegment],
        *,
        platform: str,
        names: dict[tuple[str, str], str],
        active: set[int],
    ) -> str:
        return "".join(
            self._render_segment(
                segment,
                platform=platform,
                names=names,
                active=active,
            )
            for segment in _ordered_segments(segments)
        )

    def _render_segment(
        self,
        segment: MessageSegment,
        *,
        platform: str,
        names: dict[tuple[str, str], str],
        active: set[int],
    ) -> str:
        if isinstance(segment, TextSegment):
            return segment.text or ""
        if isinstance(segment, AtSegment):
            if segment.is_all:
                return "@全体成员"
            if segment.target is None:
                return "@用户"
            return "@" + names.get((platform, segment.target), "用户")
        if isinstance(segment, ImageSegment):
            return (
                f"[图片: {_single_line(segment.summary)}]"
                if segment.summary
                else "[图片]"
            )
        if isinstance(segment, FileSegment):
            return (
                f"[文件: {_single_line(segment.name)}]"
                if segment.name
                else "[文件]"
            )
        if isinstance(segment, UnknownSegment):
            return (
                f"[未知消息类型: {_single_line(segment.original_type)}]"
                if segment.original_type is not None
                else "[未知消息类型]"
            )
        if isinstance(segment, ReplySegment):
            return self._render_reply(
                segment,
                names=names,
                active=active,
            )
        if isinstance(segment, ForwardSegment):
            return self._render_forward(
                segment,
                platform=platform,
                names=names,
                active=active,
            )
        raise TypeError(f"unsupported message segment type: {type(segment).__name__}")

    def _render_reply(
        self,
        segment: ReplySegment,
        *,
        names: dict[tuple[str, str], str],
        active: set[int],
    ) -> str:
        resolved = segment.resolved_message
        if resolved is None:
            if segment.referenced_message_id is None:
                return "[回复消息，引用ID不可用]"
            if self._include_platform_ids:
                return f"[回复消息 {_single_line(segment.referenced_message_id)}]"
            return "[回复某条消息]"

        key = id(resolved)
        if key in active:
            return RECURSION_MARKER
        active.add(key)
        try:
            quoted = self._render_resolved_message(
                resolved,
                names=names,
                active=active,
            )
        finally:
            active.remove(key)
        label = _identity_label(resolved.author)
        if "\n" not in quoted:
            return f"[回复 {label}: {quoted or '[空消息]'}]"
        quoted_lines = "\n".join(f"  > {line}" for line in quoted.split("\n"))
        return f"[回复 {label}]\n{quoted_lines}"

    def _render_resolved_message(
        self,
        resolved: ResolvedMessageReference,
        *,
        names: dict[tuple[str, str], str],
        active: set[int],
    ) -> str:
        return self._render_segments(
            resolved.segments,
            platform=resolved.author.platform,
            names=names,
            active=active,
        )

    def _render_forward(
        self,
        segment: ForwardSegment,
        *,
        platform: str,
        names: dict[tuple[str, str], str],
        active: set[int],
    ) -> str:
        key = id(segment)
        if key in active:
            return RECURSION_MARKER
        active.add(key)
        try:
            content = self._render_segments(
                segment.content,
                platform=platform,
                names=names,
                active=active,
            )
            nodes = tuple(segment.nodes)
            if not segment.resolved and not nodes:
                lines = ["[合并转发：内容未解析]"]
            else:
                lines = ["[合并转发]"]
            if content:
                lines.extend(_labeled_block("  内容: ", content))
            for node in nodes:
                lines.extend(
                    self._render_forward_node(
                        node,
                        names=names,
                        active=active,
                    )
                )
            return "\n".join(lines)
        finally:
            active.remove(key)

    def _render_forward_node(
        self,
        node: ForwardNode,
        *,
        names: dict[tuple[str, str], str],
        active: set[int],
    ) -> list[str]:
        body = self._render_segments(
            node.content,
            platform=node.sender.platform,
            names=names,
            active=active,
        ) or "[空消息]"
        label = _identity_label(node.sender)
        if "\n" not in body:
            return [f"  - {label}: {body}"]
        return [f"  - {label}:", *[f"      {line}" for line in body.split("\n")]]

    def _truncate(self, text: str) -> str:
        if self._max_chars is None or len(text) <= self._max_chars:
            return text
        keep = self._max_chars - len(TRUNCATION_MARKER)
        return text[:keep] + TRUNCATION_MARKER


def _ordered_segments(segments: Sequence[MessageSegment]) -> list[MessageSegment]:
    return [
        segment
        for _, segment in sorted(
            enumerate(segments),
            key=lambda item: (item[1].position, item[0]),
        )
    ]


def _identity_label(identity: IdentityRef) -> str:
    if identity.availability is IdentityAvailability.UNKNOWN:
        return "[作者未知]"
    if identity.availability is IdentityAvailability.UNAVAILABLE:
        return "[原作者不可用]"
    name = identity.card or identity.display_name
    return _single_line(name) if name else "已知用户"


def _identity_names(messages: Iterable[InternalMessage]) -> dict[tuple[str, str], str]:
    names: dict[tuple[str, str], str] = {}
    visited: set[int] = set()
    for message in messages:
        _remember_identity(names, message.actor)
        _remember_identity(names, message.author)
        _collect_segment_identities(names, message.segments, visited)
    return names


def _collect_segment_identities(
    names: dict[tuple[str, str], str],
    segments: Sequence[MessageSegment],
    visited: set[int],
) -> None:
    for segment in segments:
        if isinstance(segment, ReplySegment) and segment.resolved_message is not None:
            if id(segment.resolved_message) in visited:
                continue
            visited.add(id(segment.resolved_message))
            _remember_identity(names, segment.resolved_message.author)
            _collect_segment_identities(
                names,
                segment.resolved_message.segments,
                visited,
            )
        elif isinstance(segment, ForwardSegment):
            if id(segment) in visited:
                continue
            visited.add(id(segment))
            _collect_segment_identities(names, segment.content, visited)
            for node in segment.nodes:
                _remember_identity(names, node.sender)
                _collect_segment_identities(names, node.content, visited)


def _remember_identity(
    names: dict[tuple[str, str], str],
    identity: IdentityRef,
) -> None:
    if (
        identity.availability is not IdentityAvailability.KNOWN
        or identity.user_id is None
    ):
        return
    name = identity.card or identity.display_name
    if name:
        names.setdefault((identity.platform, identity.user_id), _single_line(name))


def _labeled_block(prefix: str, content: str) -> list[str]:
    lines = content.split("\n")
    continuation = " " * len(prefix)
    return [prefix + lines[0], *[continuation + line for line in lines[1:]]]


def _single_line(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
