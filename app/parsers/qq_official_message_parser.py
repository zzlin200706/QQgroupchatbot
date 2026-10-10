"""Parse QQ Official group messages without guessing missing facts.

Structured MsgElement lists preserve independent authors and nested forwards.
Legacy 102 content remains readable platform display text inside an unresolved
ForwardSegment; its presentation is never parsed into identities or media.
Parsing performs no network I/O.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime
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
    ReplyResolutionStatus,
    ReplySegment,
    ResolvedMessageReference,
    TextSegment,
    UnknownSegment,
)


_GROUP_MESSAGE_EVENTS = frozenset({"GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"})
_REPLY_MESSAGE_TYPE = 103
_FORWARD_MESSAGE_TYPE = 102
_MENTION_TOKEN = re.compile(r"<@([^>]+)>")
_REFERENCE_KEY = re.compile(r"REFIDX_[A-Za-z0-9+/_=-]+")


@dataclass
class _ForwardParseState:
    raw_event_id: int | None
    parent_message_id: str | None
    remaining_nodes: int
    reference_depth: int = 0


class QQOfficialMessageParser:
    """Transform one QQ Official group-message event into a message.

    This parser performs no network requests and does not infer data absent from
    an event payload. It accepts both the current top-level QQ envelope and the
    repository's earlier ``gateway/data`` wrapper for backward compatibility.
    Unrelated events return ``None``.
    """

    def __init__(
        self,
        *,
        max_forward_depth: int = 8,
        max_forward_nodes: int = 1000,
    ) -> None:
        for name, value in (
            ("max_forward_depth", max_forward_depth),
            ("max_forward_nodes", max_forward_nodes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._max_forward_depth = max_forward_depth
        self._max_forward_nodes = max_forward_nodes

    def parse(
        self,
        event: Mapping[str, Any],
        *,
        raw_event_id: int | None = None,
    ) -> InternalMessage | None:
        event_type, payload = _event_payload(event)
        if event_type not in _GROUP_MESSAGE_EVENTS:
            return None
        if not isinstance(payload, Mapping):
            return None

        raw_payload = copy.deepcopy(dict(payload))
        actor = _identity(raw_payload.get("author"), present_source=IdentitySource.EVENT)
        author = _identity(raw_payload.get("author"), present_source=IdentitySource.EVENT)
        provenance = MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=raw_event_id,
        )
        message_type = _as_string(raw_payload.get("message_type"))
        return InternalMessage(
            platform="qq_official",
            source_raw_event_id=raw_event_id,
            platform_message_id=_as_string(raw_payload.get("id")),
            context=MessageContext(
                message_type=message_type,
                sub_type=event_type,
                group_id=_group_identifier(raw_payload),
            ),
            actor=actor,
            author=author,
            timestamp=_timestamp(raw_payload.get("timestamp")),
            segments=self._parse_segments(
                raw_payload,
                state=_ForwardParseState(
                    raw_event_id=raw_event_id,
                    parent_message_id=_as_string(raw_payload.get("id")),
                    remaining_nodes=self._max_forward_nodes,
                ),
                forward_depth=0,
            ),
            provenance=provenance,
        )

    def _parse_segments(
        self,
        payload: Mapping[str, Any],
        *,
        state: _ForwardParseState,
        forward_depth: int,
    ) -> tuple[MessageSegment, ...]:
        message_type = _as_int(payload.get("message_type"))
        if message_type == _FORWARD_MESSAGE_TYPE:
            return (self._forward_segment(payload, state=state, depth=forward_depth),)

        segments: list[MessageSegment] = []
        if message_type == _REPLY_MESSAGE_TYPE:
            segments.extend(
                self._reply_segments(
                    payload,
                    start_position=0,
                    state=state,
                    forward_depth=forward_depth,
                )
            )
        segments.extend(self._message_like_segments(payload, start_position=len(segments)))
        return tuple(segments)

    def _forward_segment(
        self,
        payload: Mapping[str, Any],
        *,
        state: _ForwardParseState,
        depth: int,
    ) -> ForwardSegment:
        # Display text is readable, but never evidence for fabricated nodes,
        # sender identities, timestamps or downloadable media.
        content = tuple(self._message_like_segments(payload, start_position=0))
        elements = payload.get("msg_elements")
        nodes: list[ForwardNode] = []
        status = ForwardResolutionStatus.UNRESOLVED
        if elements is not None and not isinstance(elements, list):
            status = ForwardResolutionStatus.INVALID_CONTENT
        elif elements:
            status = ForwardResolutionStatus.EMBEDDED
            if depth + state.reference_depth >= self._max_forward_depth:
                status = ForwardResolutionStatus.DEPTH_LIMIT
            else:
                for element in elements:
                    if state.remaining_nodes == 0:
                        status = ForwardResolutionStatus.NODE_LIMIT
                        break
                    state.remaining_nodes -= 1
                    if isinstance(element, Mapping):
                        author = element.get("author")
                        node_content = self._parse_segments(
                            element, state=state, forward_depth=depth + 1
                        )
                    else:
                        status = ForwardResolutionStatus.INVALID_CONTENT
                        author = None
                        node_content = (
                            UnknownSegment(
                                position=0,
                                original_type="forward_node",
                                raw_data=copy.deepcopy(element),
                            ),
                        )
                    nodes.append(
                        ForwardNode(
                            sender=_identity(
                                author,
                                present_source=IdentitySource.FORWARD_NODE,
                                missing_source=IdentitySource.FORWARD_NODE,
                            ),
                            # MsgElement has no documented timestamp field.
                            timestamp=None,
                            content=node_content,
                            provenance=MessageProvenance(
                                source_type=(
                                    ProvenanceSource.FORWARD_NODE
                                    if depth == 0
                                    else ProvenanceSource.NESTED_FORWARD_NODE
                                ),
                                raw_event_id=state.raw_event_id,
                                parent_message_id=state.parent_message_id,
                                forward_depth=depth + 1,
                            ),
                            raw_data=copy.deepcopy(element),
                        )
                    )
        return ForwardSegment(
            position=0,
            reference_id=None,
            resolved=status is ForwardResolutionStatus.EMBEDDED,
            resolution_status=status,
            content=content,
            nodes=tuple(nodes),
            raw_data=copy.deepcopy(dict(payload)),
        )

    def _reply_segments(
        self,
        payload: Mapping[str, Any],
        *,
        start_position: int,
        state: _ForwardParseState,
        forward_depth: int,
    ) -> list[ReplySegment]:
        elements = payload.get("msg_elements")
        message_scene = payload.get("message_scene")
        if not isinstance(elements, list) or not elements:
            return [
                ReplySegment(
                    position=start_position,
                    referenced_message_id=None,
                    reference_key=None,
                    resolution_status=ReplyResolutionStatus.UNRESOLVED,
                    raw_data={
                        "msg_elements": copy.deepcopy(elements),
                        "message_scene": copy.deepcopy(message_scene),
                    },
                )
            ]
        if forward_depth + state.reference_depth >= self._max_forward_depth:
            return [ReplySegment(
                position=start_position, referenced_message_id=None,
                resolution_status=ReplyResolutionStatus.DEPTH_LIMIT,
                raw_data={"msg_elements": copy.deepcopy(elements)},
            )]
        replies: list[ReplySegment] = []
        for index, element in enumerate(elements):
            if state.remaining_nodes == 0:
                replies.append(ReplySegment(
                    position=start_position + index, referenced_message_id=None,
                    resolution_status=ReplyResolutionStatus.NODE_LIMIT,
                    raw_data={"msg_elements": copy.deepcopy(elements[index:])},
                ))
                break
            state.remaining_nodes -= 1
            replies.append(ReplySegment(
                position=start_position + index,
                # The observed ref_msg_idx/msg_idx values are opaque REFIDX
                # tokens, not Gateway message IDs.
                referenced_message_id=None,
                reference_key=_reply_reference_key(
                    element,
                    message_scene=message_scene,
                    allow_scene_fallback=len(elements) == 1,
                ),
                resolution_status=ReplyResolutionStatus.UNRESOLVED,
                resolved_message=self._resolved_reply_message(
                    element, state=state, forward_depth=forward_depth
                ),
                raw_data={
                    "msg_element": copy.deepcopy(element),
                    "message_scene": copy.deepcopy(message_scene),
                },
            ))
        return replies

    def _resolved_reply_message(
        self,
        element: object,
        *,
        state: _ForwardParseState,
        forward_depth: int,
    ) -> ResolvedMessageReference | None:
        if not isinstance(element, Mapping):
            return None
        state.reference_depth += 1
        try:
            if (_as_int(element.get("message_type")) == _REPLY_MESSAGE_TYPE
                    and not element.get("msg_elements")):
                # Real 103 MsgElements may already contain the quoted body,
                # with no deeper reference. Do not invent another empty reply.
                segments = tuple(self._message_like_segments(element, start_position=0))
            else:
                segments = self._parse_segments(
                    element, state=state, forward_depth=forward_depth,
                )
        finally:
            state.reference_depth -= 1
        if not segments:
            return None
        return ResolvedMessageReference(
            platform_message_id=_as_string(element.get("id")),
            author=_identity(
                element.get("author"),
                present_source=IdentitySource.RESOLVED_MESSAGE,
                missing_source=IdentitySource.RESOLVED_MESSAGE,
            ),
            timestamp=_timestamp(element.get("timestamp")),
            segments=segments,
            raw_data=copy.deepcopy(dict(element)),
        )

    def _message_like_segments(
        self,
        payload: Mapping[str, Any],
        *,
        start_position: int,
    ) -> list[MessageSegment]:
        segments = self._content_segments(payload, start_position=start_position)
        segments.extend(self._attachment_segments(payload, start_position=start_position + len(segments)))
        return segments

    def _content_segments(
        self,
        payload: Mapping[str, Any],
        *,
        start_position: int,
    ) -> list[MessageSegment]:
        content = payload.get("content")
        if content is None or content == "":
            return []
        if not isinstance(content, str):
            return [
                UnknownSegment(
                    position=start_position,
                    original_type="unstructured_content",
                    raw_data=copy.deepcopy(content),
                )
            ]

        mentions = payload.get("mentions")
        mention_by_id = {
            mention_id: mention
            for mention in (mentions if isinstance(mentions, list) else [])
            if isinstance(mention, Mapping)
            if (mention_id := _as_string(mention.get("id"))) is not None
        }
        segments: list[MessageSegment] = []
        cursor = 0
        matched_ids: set[str] = set()
        for match in _MENTION_TOKEN.finditer(content):
            target = match.group(1)
            mention = mention_by_id.get(target)
            if mention is None:
                continue
            if match.start() > cursor:
                segments.append(
                    TextSegment(
                        position=start_position + len(segments),
                        text=content[cursor : match.start()],
                        raw_data=content[cursor : match.start()],
                    )
                )
            segments.append(
                AtSegment(
                    position=start_position + len(segments),
                    target=target,
                    is_all=False,
                    display_name=(
                        _as_string(mention.get("nickname"))
                        or _as_string(mention.get("username"))
                    ),
                    is_self=(
                        mention.get("is_you")
                        if isinstance(mention.get("is_you"), bool)
                        else None
                    ),
                    raw_data={"token": match.group(0), "mention": copy.deepcopy(dict(mention))},
                )
            )
            matched_ids.add(target)
            cursor = match.end()
        if cursor < len(content) or not segments:
            segments.append(
                TextSegment(
                    position=start_position + len(segments),
                    text=content[cursor:],
                    raw_data=content[cursor:],
                )
            )
        for mention_id, mention in mention_by_id.items():
            if mention_id not in matched_ids:
                segments.append(
                    UnknownSegment(
                        position=start_position + len(segments),
                        original_type="unmatched_mention",
                        raw_data=copy.deepcopy(dict(mention)),
                    )
                )
        return segments

    def _attachment_segments(
        self,
        payload: Mapping[str, Any],
        *,
        start_position: int,
    ) -> list[MessageSegment]:
        attachments = payload.get("attachments")
        if not isinstance(attachments, list):
            return []
        segments: list[MessageSegment] = []
        for attachment in attachments:
            position = start_position + len(segments)
            if not isinstance(attachment, Mapping):
                segments.append(
                    UnknownSegment(position=position, original_type="attachment", raw_data=copy.deepcopy(attachment))
                )
                continue
            raw_attachment = copy.deepcopy(dict(attachment))
            content_type = _as_string(attachment.get("content_type"))
            if content_type is not None and content_type.startswith("image/"):
                segments.append(
                    ImageSegment(
                        position=position,
                        file=_as_string(attachment.get("filename")),
                        url=_as_string(attachment.get("url")),
                        summary=None,
                        sub_type=None,
                        file_size=_as_int(attachment.get("size")),
                        content_type=content_type,
                        width=_as_int(attachment.get("width")),
                        height=_as_int(attachment.get("height")),
                        description=None,
                        raw_data=raw_attachment,
                    )
                )
            elif content_type == "file":
                segments.append(
                    FileSegment(
                        position=position,
                        file=None,
                        name=_as_string(attachment.get("filename")),
                        file_id=None,
                        file_size=_as_int(attachment.get("size")),
                        url=_as_string(attachment.get("url")),
                        path=None,
                        raw_data=raw_attachment,
                    )
                )
            else:
                segments.append(
                    UnknownSegment(
                        position=position,
                        original_type=f"attachment:{content_type}" if content_type else "attachment",
                        raw_data=raw_attachment,
                    )
                )
        return segments


def _reply_reference_key(
    element: object,
    *,
    message_scene: object,
    allow_scene_fallback: bool,
) -> str | None:
    if not isinstance(element, Mapping):
        return None
    element_value = element.get("msg_idx")
    element_key = _valid_reference_key(element_value)
    scene_keys = _scene_reply_reference_keys(message_scene)

    # A malformed element value or conflicting target evidence is unresolved.
    if element_value is not None and element_key is None:
        return None
    if element_key is not None:
        if scene_keys and scene_keys != (element_key,):
            return None
        return element_key
    if allow_scene_fallback and len(scene_keys) == 1:
        return scene_keys[0]
    return None


def _scene_reply_reference_keys(message_scene: object) -> tuple[str, ...]:
    if not isinstance(message_scene, Mapping):
        return ()
    ext = message_scene.get("ext")
    if not isinstance(ext, list):
        return ()
    keys: list[str] = []
    for item in ext:
        if not isinstance(item, str):
            continue
        name, separator, value = item.partition("=")
        if separator != "=" or name != "ref_msg_idx":
            continue
        key = _valid_reference_key(value)
        if key is not None and key not in keys:
            keys.append(key)
    return tuple(keys)


def _valid_reference_key(value: object) -> str | None:
    if not isinstance(value, str) or _REFERENCE_KEY.fullmatch(value) is None:
        return None
    return value


def _event_payload(event: Mapping[str, Any]) -> tuple[str | None, object]:
    if "t" in event or "d" in event:
        return _as_string(event.get("t")), event.get("d")
    gateway = event.get("gateway")
    payload = event.get("data")
    if not isinstance(gateway, Mapping):
        return None, payload
    return _as_string(gateway.get("t")), payload


def _identity(
    value: object,
    *,
    present_source: IdentitySource,
    missing_source: IdentitySource = IdentitySource.UNKNOWN,
) -> IdentityRef:
    author = value if isinstance(value, Mapping) else {}
    user_id = _as_string(author.get("id")) or _as_string(author.get("member_openid"))
    return IdentityRef(
        platform="qq_official",
        user_id=user_id,
        display_name=_as_string(author.get("username")),
        card=None,
        source=present_source if user_id is not None else missing_source,
        availability=IdentityAvailability.KNOWN if user_id is not None else IdentityAvailability.UNAVAILABLE,
    )


def _group_identifier(payload: Mapping[str, Any]) -> str | None:
    return _as_string(payload.get("group_openid")) or _as_string(payload.get("group_id"))


def _as_string(value: object) -> str | None:
    if value is None or isinstance(value, (bool, Mapping, list, tuple)):
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _as_int(value: object) -> int | None:
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
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
