"""Optional network enrichment for parsed reply and forward references."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol

from app.adapters.onebot.client import OneBotClientError
from app.domain.messages.models import InternalMessage
from app.domain.messages.segments import (
    ForwardNode,
    ForwardResolutionStatus,
    ForwardSegment,
    MessageSegment,
    ReplyResolutionStatus,
    ReplySegment,
    ResolvedMessageReference,
)
from app.parsers.onebot_message_parser import OneBotMessageParser


logger = logging.getLogger(__name__)


class ReferenceActionClient(Protocol):
    """The minimal OneBot action surface used by reference enrichment."""

    async def get_message(self, message_id: str | int) -> dict[str, object]: ...

    async def get_forward_message(self, message_id: str) -> dict[str, object]: ...


@dataclass(frozen=True)
class _ReplyLookup:
    status: ReplyResolutionStatus
    resolved_message: ResolvedMessageReference | None = None
    resolved_raw_data: Any = None


@dataclass(frozen=True)
class _ForwardLookup:
    status: ForwardResolutionStatus
    parsed_forward: ForwardSegment | None = None
    resolved_raw_data: Any = None


class ReferenceEnrichmentService:
    """Return an enriched copy of an InternalMessage without mutating it.

    Resolution is deliberately sequential and cached only for the lifetime of a
    single ``enrich`` call. Nested unresolved forwards are never fetched here;
    Phase 6 owns recursive forward network traversal.
    """

    def __init__(
        self,
        *,
        onebot_client: ReferenceActionClient,
        parser: OneBotMessageParser,
    ) -> None:
        self._onebot_client = onebot_client
        self._parser = parser

    async def enrich(self, message: InternalMessage) -> InternalMessage:
        """Resolve optional direct references and return a new message object."""

        reply_cache: dict[str, _ReplyLookup] = {}
        forward_cache: dict[str, _ForwardLookup] = {}
        segments = await self._enrich_segments(
            message.segments,
            source_raw_event_id=message.source_raw_event_id,
            reply_cache=reply_cache,
            forward_cache=forward_cache,
            allow_forward_fetch=True,
        )
        return replace(message, segments=segments)

    async def _enrich_segments(
        self,
        segments: tuple[MessageSegment, ...],
        *,
        source_raw_event_id: int | None,
        reply_cache: dict[str, _ReplyLookup],
        forward_cache: dict[str, _ForwardLookup],
        allow_forward_fetch: bool,
    ) -> tuple[MessageSegment, ...]:
        enriched: list[MessageSegment] = []
        for segment in segments:
            if isinstance(segment, ReplySegment):
                enriched.append(
                    await self._enrich_reply(
                        segment,
                        source_raw_event_id=source_raw_event_id,
                        reply_cache=reply_cache,
                    )
                )
            elif isinstance(segment, ForwardSegment):
                enriched.append(
                    await self._enrich_forward(
                        segment,
                        source_raw_event_id=source_raw_event_id,
                        reply_cache=reply_cache,
                        forward_cache=forward_cache,
                        allow_forward_fetch=allow_forward_fetch,
                    )
                )
            else:
                enriched.append(segment)
        return tuple(enriched)

    async def _enrich_reply(
        self,
        segment: ReplySegment,
        *,
        source_raw_event_id: int | None,
        reply_cache: dict[str, _ReplyLookup],
    ) -> ReplySegment:
        reference_id = segment.referenced_message_id
        if reference_id is None:
            return replace(segment, resolution_status=ReplyResolutionStatus.INVALID_REFERENCE)

        lookup = reply_cache.get(reference_id)
        if lookup is None:
            lookup = await self._fetch_reply(reference_id, source_raw_event_id)
            reply_cache[reference_id] = lookup
        return replace(
            segment,
            resolution_status=lookup.status,
            resolved_message=lookup.resolved_message,
            resolved_raw_data=copy.deepcopy(lookup.resolved_raw_data),
        )

    async def _fetch_reply(
        self,
        reference_id: str,
        source_raw_event_id: int | None,
    ) -> _ReplyLookup:
        try:
            response = await self._onebot_client.get_message(reference_id)
        except OneBotClientError as error:
            logger.warning(
                "reference enrichment failed reference_type=reply resolution=fetch_failed error_type=%s",
                type(error).__name__,
            )
            return _ReplyLookup(status=ReplyResolutionStatus.FETCH_FAILED)
        except Exception as error:
            logger.warning(
                "reference enrichment failed reference_type=reply resolution=fetch_failed error_type=%s",
                type(error).__name__,
            )
            return _ReplyLookup(status=ReplyResolutionStatus.FETCH_FAILED)

        outcome, data = _successful_mapping_data(response)
        if outcome == "fetch_failed":
            return _ReplyLookup(status=ReplyResolutionStatus.FETCH_FAILED)
        if data is None:
            return _ReplyLookup(status=ReplyResolutionStatus.INVALID_RESPONSE)
        parsed = self._parser.parse_resolved_message_reference(
            data,
            source_raw_event_id=source_raw_event_id,
        )
        if parsed is None:
            return _ReplyLookup(status=ReplyResolutionStatus.INVALID_RESPONSE)
        return _ReplyLookup(
            status=ReplyResolutionStatus.RESOLVED,
            resolved_message=parsed,
            resolved_raw_data=copy.deepcopy(data),
        )

    async def _enrich_forward(
        self,
        segment: ForwardSegment,
        *,
        source_raw_event_id: int | None,
        reply_cache: dict[str, _ReplyLookup],
        forward_cache: dict[str, _ForwardLookup],
        allow_forward_fetch: bool,
    ) -> ForwardSegment:
        enriched = segment
        if (
            allow_forward_fetch
            and segment.resolution_status is ForwardResolutionStatus.UNRESOLVED
        ):
            reference_id = segment.reference_id
            if reference_id is None:
                enriched = replace(
                    segment,
                    resolution_status=ForwardResolutionStatus.INVALID_REFERENCE,
                )
            else:
                lookup = forward_cache.get(reference_id)
                if lookup is None:
                    lookup = await self._fetch_forward(reference_id, source_raw_event_id)
                    forward_cache[reference_id] = lookup
                if lookup.parsed_forward is not None:
                    enriched = replace(
                        segment,
                        resolved=True,
                        resolution_status=lookup.status,
                        content=lookup.parsed_forward.content,
                        nodes=lookup.parsed_forward.nodes,
                        resolved_raw_data=copy.deepcopy(lookup.resolved_raw_data),
                    )
                else:
                    enriched = replace(segment, resolution_status=lookup.status)

        content = await self._enrich_segments(
            enriched.content,
            source_raw_event_id=source_raw_event_id,
            reply_cache=reply_cache,
            forward_cache=forward_cache,
            allow_forward_fetch=False,
        )
        nodes = await self._enrich_nodes(
            enriched.nodes,
            source_raw_event_id=source_raw_event_id,
            reply_cache=reply_cache,
            forward_cache=forward_cache,
        )
        return replace(enriched, content=content, nodes=nodes)

    async def _enrich_nodes(
        self,
        nodes: tuple[ForwardNode, ...],
        *,
        source_raw_event_id: int | None,
        reply_cache: dict[str, _ReplyLookup],
        forward_cache: dict[str, _ForwardLookup],
    ) -> tuple[ForwardNode, ...]:
        enriched: list[ForwardNode] = []
        for node in nodes:
            content = await self._enrich_segments(
                node.content,
                source_raw_event_id=source_raw_event_id,
                reply_cache=reply_cache,
                forward_cache=forward_cache,
                allow_forward_fetch=False,
            )
            enriched.append(replace(node, content=content))
        return tuple(enriched)

    async def _fetch_forward(
        self,
        reference_id: str,
        source_raw_event_id: int | None,
    ) -> _ForwardLookup:
        try:
            response = await self._onebot_client.get_forward_message(reference_id)
        except OneBotClientError as error:
            logger.warning(
                "reference enrichment failed reference_type=forward resolution=fetch_failed error_type=%s",
                type(error).__name__,
            )
            return _ForwardLookup(status=ForwardResolutionStatus.FETCH_FAILED)
        except Exception as error:
            logger.warning(
                "reference enrichment failed reference_type=forward resolution=fetch_failed error_type=%s",
                type(error).__name__,
            )
            return _ForwardLookup(status=ForwardResolutionStatus.FETCH_FAILED)

        outcome, data = _successful_mapping_data(response)
        if outcome == "fetch_failed":
            return _ForwardLookup(status=ForwardResolutionStatus.FETCH_FAILED)
        if data is None:
            return _ForwardLookup(status=ForwardResolutionStatus.INVALID_RESPONSE)
        content = data.get("messages")
        if not isinstance(content, list):
            return _ForwardLookup(status=ForwardResolutionStatus.INVALID_RESPONSE)
        parsed = self._parser.parse_forward_content(
            content,
            source_raw_event_id=source_raw_event_id,
            reference_id=reference_id,
        )
        if parsed.resolution_status is not ForwardResolutionStatus.EMBEDDED:
            return _ForwardLookup(status=ForwardResolutionStatus.INVALID_RESPONSE)
        return _ForwardLookup(
            status=ForwardResolutionStatus.FETCHED,
            parsed_forward=parsed,
            resolved_raw_data=copy.deepcopy(data),
        )


def _successful_mapping_data(
    response: object,
) -> tuple[str, Mapping[str, Any] | None]:
    """Validate a OneBot action envelope without logging response contents."""

    if not isinstance(response, Mapping):
        return "invalid_response", None
    if response.get("status") != "ok" or response.get("retcode") != 0:
        return "fetch_failed", None
    data = response.get("data")
    if not isinstance(data, Mapping):
        return "invalid_response", None
    return "success", data
