"""Domain-level message, identity, and provenance models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.messages.segments import MessageSegment


class IdentityAvailability(str, Enum):
    """Whether the platform supplied a usable identity for this role."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class IdentitySource(str, Enum):
    """Where an identity was explicitly supplied, never inferred."""

    EVENT = "event"
    FORWARD_NODE = "forward_node"
    RESOLVED_MESSAGE = "resolved_message"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IdentityRef:
    """A platform identity with explicit availability and source."""

    platform: str
    user_id: str | None
    display_name: str | None
    card: str | None
    source: IdentitySource
    availability: IdentityAvailability


class ProvenanceSource(str, Enum):
    """The source context for a message or a forward node."""

    DIRECT_EVENT = "direct_event"
    FORWARD_NODE = "forward_node"
    NESTED_FORWARD_NODE = "nested_forward_node"


@dataclass(frozen=True)
class MessageProvenance:
    """Trace a parsed object to its receipt and parent forward context."""

    source_type: ProvenanceSource
    raw_event_id: int | None
    parent_message_id: str | None = None
    forward_depth: int = 0


@dataclass(frozen=True)
class MessageContext:
    """Top-level context copied from a message event without interpretation."""

    message_type: str | None
    sub_type: str | None
    group_id: str | None


@dataclass(frozen=True)
class InternalMessage:
    """The parser's stable representation of one direct OneBot message event."""

    platform: str
    source_raw_event_id: int | None
    platform_message_id: str | None
    context: MessageContext
    actor: IdentityRef
    author: IdentityRef
    timestamp: datetime | None
    segments: tuple[MessageSegment, ...]
    provenance: MessageProvenance
