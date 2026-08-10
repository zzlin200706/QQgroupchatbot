"""Parse OneBot message event payloads without mutating their raw JSON."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from app.domain.messages.models import (
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
    InternalMessage,
    MessageContext,
    MessageProvenance,
    ProvenanceSource,
)
from app.domain.messages.segments import (
    AtSegment,
    FileSegment,
    ForwardNode,
    ForwardResolutionStatus,
    ForwardSegment,
    ImageSegment,
    MessageSegment,
    ReplySegment,
    TextSegment,
    UnknownSegment,
)


@dataclass
class _ParseBudget:
    forward_nodes: int = 0


class OneBotMessageParser:
    """Transform one stored OneBot message event into an InternalMessage.

    It only consumes the payload already received by the adapter. In particular,
    it does not call `get_forward_msg`, resolve replies, download media, or infer
    authors from an outer forwarder.
    """

    def __init__(
        self,
        *,
        max_forward_depth: int = 10,
        max_forward_nodes: int = 500,
        max_message_segments: int = 1000,
    ) -> None:
        if max_forward_depth < 1:
            raise ValueError("max_forward_depth must be at least one")
        if max_forward_nodes < 1:
            raise ValueError("max_forward_nodes must be at least one")
        if max_message_segments < 1:
            raise ValueError("max_message_segments must be at least one")
        self._max_forward_depth = max_forward_depth
        self._max_forward_nodes = max_forward_nodes
        self._max_message_segments = max_message_segments

    def parse(
        self,
        payload: Mapping[str, Any],
        *,
        source_raw_event_id: int | None,
    ) -> InternalMessage | None:
        """Return a message model, or ``None`` for any non-message event."""

        if payload.get("post_type") != "message":
            return None

        event = copy.deepcopy(dict(payload))
        actor = _event_identity(event)
        author = _event_identity(event)
        context = MessageContext(
            message_type=_as_string(event.get("message_type")),
            sub_type=_as_string(event.get("sub_type")),
            group_id=_as_string(event.get("group_id")),
        )
        provenance = MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=source_raw_event_id,
        )
        budget = _ParseBudget()
        raw_segments = event.get("message")
        segments = self._parse_message_segments(
            raw_segments,
            budget=budget,
            provenance=provenance,
            forward_depth=0,
        )
        return InternalMessage(
            platform="onebot11",
            source_raw_event_id=source_raw_event_id,
            platform_message_id=_as_string(event.get("message_id")),
            context=context,
            actor=actor,
            author=author,
            timestamp=_timestamp(event.get("time")),
            segments=segments,
            provenance=provenance,
        )

    def _parse_message_segments(
        self,
        raw_segments: object,
        *,
        budget: _ParseBudget,
        provenance: MessageProvenance,
        forward_depth: int,
    ) -> tuple[MessageSegment, ...]:
        if not isinstance(raw_segments, list):
            return (
                UnknownSegment(
                    position=0,
                    original_type="unstructured_message",
                    raw_data=copy.deepcopy(raw_segments),
                ),
            )

        parsed: list[MessageSegment] = []
        for position, raw_segment in enumerate(raw_segments):
            if position >= self._max_message_segments:
                parsed.append(
                    UnknownSegment(
                        position=position,
                        original_type="segment_limit",
                        raw_data=copy.deepcopy(raw_segments[position:]),
                    )
                )
                break
            parsed.append(
                self._parse_segment(
                    raw_segment,
                    position=position,
                    budget=budget,
                    provenance=provenance,
                    forward_depth=forward_depth,
                )
            )
        return tuple(parsed)

    def _parse_segment(
        self,
        raw_segment: object,
        *,
        position: int,
        budget: _ParseBudget,
        provenance: MessageProvenance,
        forward_depth: int,
    ) -> MessageSegment:
        if not isinstance(raw_segment, Mapping):
            return UnknownSegment(
                position=position,
                original_type=None,
                raw_data=copy.deepcopy(raw_segment),
            )

        segment_type = _as_string(raw_segment.get("type"))
        raw_data = copy.deepcopy(raw_segment.get("data"))
        data = raw_segment.get("data")
        mapping_data: Mapping[str, Any] = data if isinstance(data, Mapping) else {}

        if segment_type == "text":
            return TextSegment(position=position, text=_as_string(mapping_data.get("text")), raw_data=raw_data)
        if segment_type == "at":
            target = _as_string(mapping_data.get("qq"))
            return AtSegment(
                position=position,
                target=target,
                is_all=target == "all",
                raw_data=raw_data,
            )
        if segment_type == "image":
            return ImageSegment(
                position=position,
                file=_as_string(mapping_data.get("file")),
                url=_as_string(mapping_data.get("url")),
                summary=_as_string(mapping_data.get("summary")),
                sub_type=_as_string(mapping_data.get("sub_type")),
                file_size=_as_integer(mapping_data.get("file_size")),
                raw_data=raw_data,
            )
        if segment_type == "reply":
            return ReplySegment(
                position=position,
                referenced_message_id=_as_string(mapping_data.get("id")),
                raw_data=raw_data,
            )
        if segment_type == "file":
            return FileSegment(
                position=position,
                file=_as_string(mapping_data.get("file")),
                name=_as_string(mapping_data.get("name")),
                file_id=_as_string(mapping_data.get("file_id")),
                file_size=_as_integer(mapping_data.get("file_size")),
                url=_as_string(mapping_data.get("url")),
                path=_as_string(mapping_data.get("path")),
                raw_data=raw_data,
            )
        if segment_type == "forward":
            return self._parse_forward(
                mapping_data,
                raw_data=raw_data,
                position=position,
                budget=budget,
                provenance=provenance,
                forward_depth=forward_depth,
            )
        return UnknownSegment(
            position=position,
            original_type=segment_type,
            raw_data=raw_data,
        )

    def _parse_forward(
        self,
        data: Mapping[str, Any],
        *,
        raw_data: Any,
        position: int,
        budget: _ParseBudget,
        provenance: MessageProvenance,
        forward_depth: int,
    ) -> ForwardSegment:
        reference_id = _as_string(data.get("id"))
        raw_content = data.get("content")
        if raw_content is None:
            return ForwardSegment(
                position=position,
                reference_id=reference_id,
                resolved=False,
                resolution_status=ForwardResolutionStatus.UNRESOLVED,
                content=(),
                nodes=(),
                raw_data=raw_data,
            )
        if not isinstance(raw_content, list):
            return ForwardSegment(
                position=position,
                reference_id=reference_id,
                resolved=False,
                resolution_status=ForwardResolutionStatus.INVALID_CONTENT,
                content=(),
                nodes=(),
                raw_data=raw_data,
            )
        if forward_depth >= self._max_forward_depth:
            return ForwardSegment(
                position=position,
                reference_id=reference_id,
                resolved=False,
                resolution_status=ForwardResolutionStatus.DEPTH_LIMIT,
                content=(),
                nodes=(),
                raw_data=raw_data,
            )

        content: list[MessageSegment] = []
        nodes: list[ForwardNode] = []
        for content_position, item in enumerate(raw_content):
            if _is_node_segment(item):
                if budget.forward_nodes >= self._max_forward_nodes:
                    return ForwardSegment(
                        position=position,
                        reference_id=reference_id,
                        resolved=False,
                        resolution_status=ForwardResolutionStatus.NODE_LIMIT,
                        content=tuple(content),
                        nodes=tuple(nodes),
                        raw_data=raw_data,
                    )
                budget.forward_nodes += 1
                nodes.append(
                    self._parse_forward_node(
                        item,
                        budget=budget,
                        parent_provenance=provenance,
                        forward_depth=forward_depth + 1,
                    )
                )
            else:
                content.append(
                    self._parse_segment(
                        item,
                        position=content_position,
                        budget=budget,
                        provenance=provenance,
                        forward_depth=forward_depth + 1,
                    )
                )
        return ForwardSegment(
            position=position,
            reference_id=reference_id,
            resolved=True,
            resolution_status=ForwardResolutionStatus.EMBEDDED,
            content=tuple(content),
            nodes=tuple(nodes),
            raw_data=raw_data,
        )

    def _parse_forward_node(
        self,
        raw_node: Mapping[str, Any],
        *,
        budget: _ParseBudget,
        parent_provenance: MessageProvenance,
        forward_depth: int,
    ) -> ForwardNode:
        data = raw_node.get("data")
        node_data: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
        source_type = (
            ProvenanceSource.FORWARD_NODE
            if forward_depth == 1
            else ProvenanceSource.NESTED_FORWARD_NODE
        )
        provenance = MessageProvenance(
            source_type=source_type,
            raw_event_id=parent_provenance.raw_event_id,
            parent_message_id=_as_string(node_data.get("id")),
            forward_depth=forward_depth,
        )
        return ForwardNode(
            sender=_node_identity(node_data),
            timestamp=_timestamp(node_data.get("time")),
            content=self._parse_message_segments(
                node_data.get("content"),
                budget=budget,
                provenance=provenance,
                forward_depth=forward_depth,
            ),
            provenance=provenance,
            raw_data=copy.deepcopy(node_data),
        )


def _event_identity(event: Mapping[str, Any]) -> IdentityRef:
    sender = event.get("sender")
    sender_data: Mapping[str, Any] = sender if isinstance(sender, Mapping) else {}
    user_id = _as_string(event.get("user_id"))
    return IdentityRef(
        platform="onebot11",
        user_id=user_id,
        display_name=_as_string(sender_data.get("nickname")),
        card=_as_string(sender_data.get("card")),
        source=IdentitySource.EVENT if user_id is not None else IdentitySource.UNKNOWN,
        availability=(IdentityAvailability.KNOWN if user_id is not None else IdentityAvailability.UNAVAILABLE),
    )


def _node_identity(node_data: Mapping[str, Any]) -> IdentityRef:
    user_id = _as_string(node_data.get("user_id"))
    return IdentityRef(
        platform="onebot11",
        user_id=user_id,
        display_name=_as_string(node_data.get("nickname")),
        card=_as_string(node_data.get("card")),
        source=(IdentitySource.FORWARD_NODE if user_id is not None else IdentitySource.UNKNOWN),
        availability=(IdentityAvailability.KNOWN if user_id is not None else IdentityAvailability.UNAVAILABLE),
    )


def _as_string(value: object) -> str | None:
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _as_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _is_node_segment(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("type") == "node"
