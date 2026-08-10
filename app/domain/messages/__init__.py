"""Internal message model and segment types."""

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
    ReplyResolutionStatus,
    ReplySegment,
    ResolvedMessageReference,
    TextSegment,
    UnknownSegment,
)

__all__ = [
    "AtSegment",
    "FileSegment",
    "ForwardNode",
    "ForwardResolutionStatus",
    "ForwardSegment",
    "IdentityAvailability",
    "IdentityRef",
    "IdentitySource",
    "ImageSegment",
    "InternalMessage",
    "MessageContext",
    "MessageProvenance",
    "ProvenanceSource",
    "ReplyResolutionStatus",
    "ReplySegment",
    "ResolvedMessageReference",
    "TextSegment",
    "UnknownSegment",
]
