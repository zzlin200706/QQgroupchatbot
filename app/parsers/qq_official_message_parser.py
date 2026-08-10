"""Parse captured QQ Official Gateway group-message dispatches losslessly.

The Phase D samples establish the currently supported shape: a Gateway dispatch
envelope with ``gateway.t == "GROUP_MESSAGE_CREATE"`` and its message payload in
``data``.  In particular, the captured ``message_type == 102`` messages contain
only QQ-rendered text, not structured forward nodes.  The parser deliberately
keeps those as unresolved :class:`ForwardSegment` values instead of extracting
authors, media, or nesting from the rendered text.
"""

from __future__ import annotations

import copy
import re
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
    ForwardResolutionStatus,
    ForwardSegment,
    ImageSegment,
    MessageSegment,
    ReplyResolutionStatus,
    ReplySegment,
    TextSegment,
    UnknownSegment,
)


_GROUP_MESSAGE_CREATE = "GROUP_MESSAGE_CREATE"
_REPLY_MESSAGE_TYPE = 103
_FORWARD_MESSAGE_TYPE = 102
_MENTION_TOKEN = re.compile(r"<@([^>]+)>")


class QQOfficialMessageParser:
    """Transform one QQ Official Gateway group-message dispatch into a message.

    This parser performs no network requests and does not infer data absent from
    a dispatch.  It accepts only the captured Gateway envelope; unrelated
    dispatches return ``None``.
    """

    def parse(
        self,
        event: Mapping[str, Any],
        *,
        raw_event_id: int | None = None,
    ) -> InternalMessage | None:
        gateway = event.get("gateway")
        payload = event.get("data")
        if not isinstance(gateway, Mapping) or gateway.get("t") != _GROUP_MESSAGE_CREATE:
            return None
        if not isinstance(payload, Mapping):
            return None

        raw_payload = copy.deepcopy(dict(payload))
        actor = _identity(raw_payload.get("author"))
        author = _identity(raw_payload.get("author"))
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
                sub_type=_as_string(gateway.get("t")),
                group_id=_as_string(raw_payload.get("group_id")),
            ),
            actor=actor,
            author=author,
            timestamp=_timestamp(raw_payload.get("timestamp")),
            segments=self._parse_segments(raw_payload),
            provenance=provenance,
        )

    def _parse_segments(self, payload: Mapping[str, Any]) -> tuple[MessageSegment, ...]:
        message_type = _as_int(payload.get("message_type"))
        if message_type == _FORWARD_MESSAGE_TYPE:
            # No captured 102 sample contains machine-readable nodes or node
            # authors.  Its content is a presentation string, so parsing it
            # would fabricate forward-node identities and tree boundaries.
            return (
                ForwardSegment(
                    position=0,
                    reference_id=None,
                    resolved=False,
                    resolution_status=ForwardResolutionStatus.UNRESOLVED,
                    content=(),
                    nodes=(),
                    raw_data=copy.deepcopy(dict(payload)),
                ),
            )

        segments: list[MessageSegment] = []
        if message_type == _REPLY_MESSAGE_TYPE:
            segments.extend(self._reply_segments(payload, start_position=len(segments)))
        segments.extend(self._content_segments(payload, start_position=len(segments)))
        segments.extend(self._attachment_segments(payload, start_position=len(segments)))
        return tuple(segments)

    def _reply_segments(
        self,
        payload: Mapping[str, Any],
        *,
        start_position: int,
    ) -> list[ReplySegment]:
        elements = payload.get("msg_elements")
        message_scene = payload.get("message_scene")
        if not isinstance(elements, list) or not elements:
            return [
                ReplySegment(
                    position=start_position,
                    referenced_message_id=None,
                    resolution_status=ReplyResolutionStatus.UNRESOLVED,
                    raw_data={
                        "msg_elements": copy.deepcopy(elements),
                        "message_scene": copy.deepcopy(message_scene),
                    },
                )
            ]
        return [
            ReplySegment(
                position=start_position + index,
                # The observed ref_msg_idx/msg_idx values are opaque REFIDX
                # tokens, not Gateway message IDs.  Preserve them in raw_data.
                referenced_message_id=None,
                resolution_status=ReplyResolutionStatus.UNRESOLVED,
                raw_data={
                    "msg_element": copy.deepcopy(element),
                    "message_scene": copy.deepcopy(message_scene),
                },
            )
            for index, element in enumerate(elements)
        ]

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


def _identity(value: object) -> IdentityRef:
    author = value if isinstance(value, Mapping) else {}
    user_id = _as_string(author.get("id"))
    return IdentityRef(
        platform="qq_official",
        user_id=user_id,
        display_name=_as_string(author.get("username")),
        card=None,
        source=IdentitySource.EVENT if user_id is not None else IdentitySource.UNKNOWN,
        availability=IdentityAvailability.KNOWN if user_id is not None else IdentityAvailability.UNAVAILABLE,
    )


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
