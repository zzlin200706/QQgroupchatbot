"""Loss-preserving internal message segment hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, TypeAlias

from app.domain.messages.models import IdentityRef, MessageProvenance


@dataclass(frozen=True)
class Segment:
    """Base information retained for every parsed segment."""

    position: int
    raw_data: Any


@dataclass(frozen=True)
class TextSegment(Segment):
    text: str | None


@dataclass(frozen=True)
class AtSegment(Segment):
    target: str | None
    is_all: bool
    display_name: str | None = None
    is_self: bool | None = None


@dataclass(frozen=True)
class ImageSegment(Segment):
    file: str | None
    url: str | None
    summary: str | None
    sub_type: str | None
    file_size: int | None


class ReplyResolutionStatus(str, Enum):
    """The outcome of optional get_msg enrichment."""

    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    INVALID_REFERENCE = "invalid_reference"
    FETCH_FAILED = "fetch_failed"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True)
class ReplySegment(Segment):
    referenced_message_id: str | None
    reference_key: str | None = None
    resolution_status: ReplyResolutionStatus = ReplyResolutionStatus.UNRESOLVED
    resolved_message: "ResolvedMessageReference | None" = None
    resolved_raw_data: Any = None


@dataclass(frozen=True)
class ResolvedMessageReference:
    """A get_msg result parsed without pretending it was a WebSocket event."""

    platform_message_id: str | None
    author: IdentityRef
    timestamp: datetime | None
    segments: tuple["MessageSegment", ...]
    raw_data: Any


@dataclass(frozen=True)
class FileSegment(Segment):
    file: str | None
    name: str | None
    file_id: str | None
    file_size: int | None
    url: str | None
    path: str | None


class ForwardResolutionStatus(str, Enum):
    """Whether inline forward content was available and safely parsed."""

    UNRESOLVED = "unresolved"
    EMBEDDED = "embedded"
    FETCHED = "fetched"
    INVALID_CONTENT = "invalid_content"
    DEPTH_LIMIT = "depth_limit"
    NODE_LIMIT = "node_limit"
    INVALID_REFERENCE = "invalid_reference"
    FETCH_FAILED = "fetch_failed"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True)
class ForwardNode:
    """A non-flattened forward node with its own independent sender."""

    sender: IdentityRef
    timestamp: datetime | None
    content: tuple[MessageSegment, ...]
    provenance: MessageProvenance
    raw_data: Any


@dataclass(frozen=True)
class ForwardSegment(Segment):
    reference_id: str | None
    resolved: bool
    resolution_status: ForwardResolutionStatus
    content: tuple[MessageSegment, ...]
    nodes: tuple[ForwardNode, ...]
    resolved_raw_data: Any = None


@dataclass(frozen=True)
class UnknownSegment(Segment):
    original_type: str | None


MessageSegment: TypeAlias = (
    TextSegment
    | AtSegment
    | ImageSegment
    | ReplySegment
    | FileSegment
    | ForwardSegment
    | UnknownSegment
)
