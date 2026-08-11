"""Explicit JSON/tree codec for normalized InternalMessage persistence."""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

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
    ReplyResolutionStatus,
    ReplySegment,
    ResolvedMessageReference,
    TextSegment,
    UnknownSegment,
)
from app.storage.models import MessageNodeRecord, MessageRecord


RELATION_SEGMENTS = "segments"
RELATION_CONTENT = "content"
RELATION_NODES = "nodes"
RELATION_RESOLVED_MESSAGE = "resolved_message"


def encode_message(
    message: InternalMessage,
    *,
    parser_name: str,
    parser_version: str,
) -> MessageRecord:
    """Build an ORM graph without relying on dataclass implementation details."""

    if message.source_raw_event_id is None:
        raise ValueError("normalized messages require source_raw_event_id")
    if not parser_name:
        raise ValueError("parser_name must not be empty")
    if not parser_version:
        raise ValueError("parser_version must not be empty")

    record = MessageRecord(
        source_raw_event_id=message.source_raw_event_id,
        parser_name=parser_name,
        parser_version=parser_version,
        platform=message.platform,
        platform_message_id=message.platform_message_id,
        message_type=message.context.message_type,
        sub_type=message.context.sub_type,
        group_id=message.context.group_id,
        timestamp=message.timestamp,
        **_identity_columns("actor", message.actor),
        **_identity_columns("author", message.author),
        **_provenance_columns(message.provenance),
    )
    for segment in message.segments:
        _encode_segment(
            segment,
            message=record,
            parent=None,
            relation=RELATION_SEGMENTS,
            depth=0,
        )
    return record


def decode_message(
    record: MessageRecord,
    nodes: Sequence[MessageNodeRecord],
) -> InternalMessage:
    """Rebuild an InternalMessage from one row and its complete node set."""

    if record.id is None:
        raise ValueError("message record must be persisted before decoding")
    node_by_id: dict[int, MessageNodeRecord] = {}
    children: dict[int | None, list[MessageNodeRecord]] = defaultdict(list)
    for node in nodes:
        if node.id is None:
            raise ValueError("message node must be persisted before decoding")
        if node.message_id != record.id:
            raise ValueError("message node belongs to a different message")
        node_by_id[node.id] = node
        children[node.parent_node_id].append(node)
    for siblings in children.values():
        siblings.sort(key=lambda item: item.position)

    decoder = _NodeDecoder(node_by_id=node_by_id, children=children)
    segments = decoder.decode_segments(None, RELATION_SEGMENTS)
    decoder.ensure_all_nodes_consumed()
    return InternalMessage(
        platform=record.platform,
        source_raw_event_id=record.source_raw_event_id,
        platform_message_id=record.platform_message_id,
        context=MessageContext(
            message_type=record.message_type,
            sub_type=record.sub_type,
            group_id=record.group_id,
        ),
        actor=_decode_identity(record, "actor"),
        author=_decode_identity(record, "author"),
        timestamp=record.timestamp,
        segments=segments,
        provenance=MessageProvenance(
            source_type=ProvenanceSource(record.provenance_source),
            raw_event_id=record.provenance_raw_event_id,
            parent_message_id=record.provenance_parent_message_id,
            forward_depth=record.provenance_forward_depth,
        ),
    )


def _encode_segment(
    segment: MessageSegment,
    *,
    message: MessageRecord,
    parent: MessageNodeRecord | None,
    relation: str,
    depth: int,
) -> MessageNodeRecord:
    kind, payload = _segment_payload(segment)
    node = _new_node(
        message=message,
        parent=parent,
        relation=relation,
        node_kind=kind,
        position=segment.position,
        depth=depth,
        payload=payload,
    )
    if isinstance(segment, ReplySegment) and segment.resolved_message is not None:
        _encode_resolved_message(
            segment.resolved_message,
            message=message,
            parent=node,
            depth=depth + 1,
        )
    elif isinstance(segment, ForwardSegment):
        for child in segment.content:
            _encode_segment(
                child,
                message=message,
                parent=node,
                relation=RELATION_CONTENT,
                depth=depth + 1,
            )
        for position, forward_node in enumerate(segment.nodes):
            _encode_forward_node(
                forward_node,
                message=message,
                parent=node,
                position=position,
                depth=depth + 1,
            )
    return node


def _encode_forward_node(
    forward_node: ForwardNode,
    *,
    message: MessageRecord,
    parent: MessageNodeRecord,
    position: int,
    depth: int,
) -> MessageNodeRecord:
    node = _new_node(
        message=message,
        parent=parent,
        relation=RELATION_NODES,
        node_kind="forward_node",
        position=position,
        depth=depth,
        payload={
            "timestamp": _encode_datetime(forward_node.timestamp),
            "raw_data": copy.deepcopy(forward_node.raw_data),
        },
        identity=forward_node.sender,
        provenance=forward_node.provenance,
    )
    for segment in forward_node.content:
        _encode_segment(
            segment,
            message=message,
            parent=node,
            relation=RELATION_CONTENT,
            depth=depth + 1,
        )
    return node


def _encode_resolved_message(
    resolved: ResolvedMessageReference,
    *,
    message: MessageRecord,
    parent: MessageNodeRecord,
    depth: int,
) -> MessageNodeRecord:
    node = _new_node(
        message=message,
        parent=parent,
        relation=RELATION_RESOLVED_MESSAGE,
        node_kind="resolved_message",
        position=0,
        depth=depth,
        payload={
            "platform_message_id": resolved.platform_message_id,
            "timestamp": _encode_datetime(resolved.timestamp),
            "raw_data": copy.deepcopy(resolved.raw_data),
        },
        identity=resolved.author,
    )
    for segment in resolved.segments:
        _encode_segment(
            segment,
            message=message,
            parent=node,
            relation=RELATION_SEGMENTS,
            depth=depth + 1,
        )
    return node


def _segment_payload(segment: MessageSegment) -> tuple[str, dict[str, Any]]:
    if isinstance(segment, TextSegment):
        return "text", {"text": segment.text, "raw_data": copy.deepcopy(segment.raw_data)}
    if isinstance(segment, AtSegment):
        return "at", {
            "target": segment.target,
            "is_all": segment.is_all,
            "display_name": segment.display_name,
            "is_self": segment.is_self,
            "raw_data": copy.deepcopy(segment.raw_data),
        }
    if isinstance(segment, ImageSegment):
        return "image", {
            "file": segment.file,
            "url": segment.url,
            "summary": segment.summary,
            "sub_type": segment.sub_type,
            "file_size": segment.file_size,
            "raw_data": copy.deepcopy(segment.raw_data),
        }
    if isinstance(segment, ReplySegment):
        return "reply", {
            "referenced_message_id": segment.referenced_message_id,
            "resolution_status": segment.resolution_status.value,
            "raw_data": copy.deepcopy(segment.raw_data),
            "resolved_raw_data": copy.deepcopy(segment.resolved_raw_data),
        }
    if isinstance(segment, FileSegment):
        return "file", {
            "file": segment.file,
            "name": segment.name,
            "file_id": segment.file_id,
            "file_size": segment.file_size,
            "url": segment.url,
            "path": segment.path,
            "raw_data": copy.deepcopy(segment.raw_data),
        }
    if isinstance(segment, ForwardSegment):
        return "forward", {
            "reference_id": segment.reference_id,
            "resolved": segment.resolved,
            "resolution_status": segment.resolution_status.value,
            "raw_data": copy.deepcopy(segment.raw_data),
            "resolved_raw_data": copy.deepcopy(segment.resolved_raw_data),
        }
    if isinstance(segment, UnknownSegment):
        return "unknown", {
            "original_type": segment.original_type,
            "raw_data": copy.deepcopy(segment.raw_data),
        }
    raise TypeError(f"unsupported message segment type: {type(segment).__name__}")


def _new_node(
    *,
    message: MessageRecord,
    parent: MessageNodeRecord | None,
    relation: str,
    node_kind: str,
    position: int,
    depth: int,
    payload: dict[str, Any],
    identity: IdentityRef | None = None,
    provenance: MessageProvenance | None = None,
) -> MessageNodeRecord:
    values: dict[str, Any] = {
        "relation": relation,
        "node_kind": node_kind,
        "position": position,
        "depth": depth,
        "payload_json": payload,
    }
    if identity is not None:
        values.update(_identity_columns("author", identity))
    if provenance is not None:
        values.update(_provenance_columns(provenance))
    node = MessageNodeRecord(**values)
    node.parent = parent
    message.nodes.append(node)
    return node


class _NodeDecoder:
    def __init__(
        self,
        *,
        node_by_id: dict[int, MessageNodeRecord],
        children: dict[int | None, list[MessageNodeRecord]],
    ) -> None:
        self._node_by_id = node_by_id
        self._children = children
        self._consumed: set[int] = set()
        self._active: set[int] = set()

    def decode_segments(
        self,
        parent_id: int | None,
        relation: str,
    ) -> tuple[MessageSegment, ...]:
        return tuple(
            self._decode_segment(node)
            for node in self._related(parent_id, relation)
        )

    def _decode_segment(self, node: MessageNodeRecord) -> MessageSegment:
        self._enter(node)
        try:
            payload = _payload(node)
            common = {"position": node.position, "raw_data": copy.deepcopy(payload.get("raw_data"))}
            if node.node_kind == "text":
                result: MessageSegment = TextSegment(text=payload.get("text"), **common)
            elif node.node_kind == "at":
                result = AtSegment(
                    target=payload.get("target"),
                    is_all=payload["is_all"],
                    display_name=payload.get("display_name"),
                    is_self=payload.get("is_self"),
                    **common,
                )
            elif node.node_kind == "image":
                result = ImageSegment(
                    file=payload.get("file"),
                    url=payload.get("url"),
                    summary=payload.get("summary"),
                    sub_type=payload.get("sub_type"),
                    file_size=payload.get("file_size"),
                    **common,
                )
            elif node.node_kind == "reply":
                resolved_nodes = self._related(node.id, RELATION_RESOLVED_MESSAGE)
                if len(resolved_nodes) > 1:
                    raise ValueError("reply contains multiple resolved messages")
                resolved = self._decode_resolved_message(resolved_nodes[0]) if resolved_nodes else None
                result = ReplySegment(
                    referenced_message_id=payload.get("referenced_message_id"),
                    resolution_status=ReplyResolutionStatus(payload["resolution_status"]),
                    resolved_message=resolved,
                    resolved_raw_data=copy.deepcopy(payload.get("resolved_raw_data")),
                    **common,
                )
            elif node.node_kind == "file":
                result = FileSegment(
                    file=payload.get("file"),
                    name=payload.get("name"),
                    file_id=payload.get("file_id"),
                    file_size=payload.get("file_size"),
                    url=payload.get("url"),
                    path=payload.get("path"),
                    **common,
                )
            elif node.node_kind == "forward":
                content = self.decode_segments(node.id, RELATION_CONTENT)
                forward_nodes = tuple(
                    self._decode_forward_node(child)
                    for child in self._related(node.id, RELATION_NODES)
                )
                result = ForwardSegment(
                    reference_id=payload.get("reference_id"),
                    resolved=payload["resolved"],
                    resolution_status=ForwardResolutionStatus(payload["resolution_status"]),
                    content=content,
                    nodes=forward_nodes,
                    resolved_raw_data=copy.deepcopy(payload.get("resolved_raw_data")),
                    **common,
                )
            elif node.node_kind == "unknown":
                result = UnknownSegment(original_type=payload.get("original_type"), **common)
            else:
                raise ValueError(f"unsupported persisted segment kind: {node.node_kind}")
            self._ensure_relations_consumed(node.id)
            return result
        finally:
            self._leave(node)

    def _decode_forward_node(self, node: MessageNodeRecord) -> ForwardNode:
        if node.node_kind != "forward_node":
            raise ValueError("nodes relation must contain forward_node objects")
        self._enter(node)
        try:
            payload = _payload(node)
            identity = _decode_optional_identity(node)
            provenance = _decode_optional_provenance(node)
            if identity is None or provenance is None:
                raise ValueError("forward node requires identity and provenance")
            result = ForwardNode(
                sender=identity,
                timestamp=_decode_datetime(payload.get("timestamp")),
                content=self.decode_segments(node.id, RELATION_CONTENT),
                provenance=provenance,
                raw_data=copy.deepcopy(payload.get("raw_data")),
            )
            self._ensure_relations_consumed(node.id)
            return result
        finally:
            self._leave(node)

    def _decode_resolved_message(self, node: MessageNodeRecord) -> ResolvedMessageReference:
        if node.node_kind != "resolved_message":
            raise ValueError("resolved_message relation contains an invalid node kind")
        self._enter(node)
        try:
            payload = _payload(node)
            identity = _decode_optional_identity(node)
            if identity is None:
                raise ValueError("resolved message requires an author identity")
            result = ResolvedMessageReference(
                platform_message_id=payload.get("platform_message_id"),
                author=identity,
                timestamp=_decode_datetime(payload.get("timestamp")),
                segments=self.decode_segments(node.id, RELATION_SEGMENTS),
                raw_data=copy.deepcopy(payload.get("raw_data")),
            )
            self._ensure_relations_consumed(node.id)
            return result
        finally:
            self._leave(node)

    def _related(self, parent_id: int | None, relation: str) -> list[MessageNodeRecord]:
        return [node for node in self._children.get(parent_id, []) if node.relation == relation]

    def _enter(self, node: MessageNodeRecord) -> None:
        if node.id in self._active:
            raise ValueError("cycle detected in message node tree")
        if node.id in self._consumed:
            raise ValueError("message node is referenced more than once")
        self._active.add(node.id)
        self._consumed.add(node.id)

    def _leave(self, node: MessageNodeRecord) -> None:
        self._active.discard(node.id)

    def _ensure_relations_consumed(self, parent_id: int) -> None:
        unexpected = [
            node.relation
            for node in self._children.get(parent_id, [])
            if node.id not in self._consumed
        ]
        if unexpected:
            raise ValueError(f"unexpected child relations: {sorted(set(unexpected))}")

    def ensure_all_nodes_consumed(self) -> None:
        if set(self._node_by_id) != self._consumed:
            raise ValueError("message node set contains orphaned or unsupported rows")


def _identity_columns(prefix: str, identity: IdentityRef) -> dict[str, Any]:
    return {
        f"{prefix}_platform": identity.platform,
        f"{prefix}_user_id": identity.user_id,
        f"{prefix}_display_name": identity.display_name,
        f"{prefix}_card": identity.card,
        f"{prefix}_source": identity.source.value,
        f"{prefix}_availability": identity.availability.value,
    }


def _provenance_columns(provenance: MessageProvenance) -> dict[str, Any]:
    return {
        "provenance_source": provenance.source_type.value,
        "provenance_raw_event_id": provenance.raw_event_id,
        "provenance_parent_message_id": provenance.parent_message_id,
        "provenance_forward_depth": provenance.forward_depth,
    }


def _decode_identity(value: object, prefix: str) -> IdentityRef:
    return IdentityRef(
        platform=getattr(value, f"{prefix}_platform"),
        user_id=getattr(value, f"{prefix}_user_id"),
        display_name=getattr(value, f"{prefix}_display_name"),
        card=getattr(value, f"{prefix}_card"),
        source=IdentitySource(getattr(value, f"{prefix}_source")),
        availability=IdentityAvailability(getattr(value, f"{prefix}_availability")),
    )


def _decode_optional_identity(node: MessageNodeRecord) -> IdentityRef | None:
    values = (
        node.author_platform,
        node.author_user_id,
        node.author_display_name,
        node.author_card,
        node.author_source,
        node.author_availability,
    )
    if all(value is None for value in values):
        return None
    if node.author_platform is None or node.author_source is None or node.author_availability is None:
        raise ValueError("persisted identity snapshot is incomplete")
    return _decode_identity(node, "author")


def _decode_optional_provenance(node: MessageNodeRecord) -> MessageProvenance | None:
    values = (
        node.provenance_source,
        node.provenance_raw_event_id,
        node.provenance_parent_message_id,
        node.provenance_forward_depth,
    )
    if all(value is None for value in values):
        return None
    if node.provenance_source is None or node.provenance_forward_depth is None:
        raise ValueError("persisted provenance is incomplete")
    return MessageProvenance(
        source_type=ProvenanceSource(node.provenance_source),
        raw_event_id=node.provenance_raw_event_id,
        parent_message_id=node.provenance_parent_message_id,
        forward_depth=node.provenance_forward_depth,
    )


def _payload(node: MessageNodeRecord) -> dict[str, Any]:
    if not isinstance(node.payload_json, dict):
        raise ValueError("message node payload must be a JSON object")
    return node.payload_json


def _encode_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decode_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("persisted datetime must be an ISO 8601 string")
    return datetime.fromisoformat(value)
