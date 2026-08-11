"""Transport-neutral QQ Official inbound event models."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from app.adapters.qq_official.gateway import QQGatewayDispatch


QQOfficialInboundTransport = Literal["websocket", "webhook"]


@dataclass(frozen=True)
class QQOfficialInboundEvent:
    """One QQ Official event envelope plus its inbound transport."""

    event_id: str | None
    op: int | None
    sequence: int | None
    event_type: str | None
    data: object
    transport: QQOfficialInboundTransport
    raw_payload: dict[str, Any]


def inbound_event_from_gateway_dispatch(
    dispatch: QQGatewayDispatch,
) -> QQOfficialInboundEvent:
    """Adapt one WebSocket dispatch into the shared inbound event model."""

    raw_payload = dispatch.raw_payload
    if raw_payload is None:
        raw_payload = {
            "id": dispatch.event_id,
            "op": dispatch.op,
            "s": dispatch.sequence,
            "t": dispatch.event_type,
            "d": copy.deepcopy(dispatch.data),
        }
    return QQOfficialInboundEvent(
        event_id=dispatch.event_id,
        op=dispatch.op,
        sequence=dispatch.sequence,
        event_type=dispatch.event_type,
        data=copy.deepcopy(dispatch.data),
        transport="websocket",
        raw_payload=copy.deepcopy(raw_payload),
    )


def inbound_event_from_webhook_payload(
    payload: Mapping[str, Any],
) -> QQOfficialInboundEvent:
    """Adapt one verified webhook callback into the shared inbound event model."""

    raw_payload = copy.deepcopy(dict(payload))
    return QQOfficialInboundEvent(
        event_id=_string_value(payload.get("id")),
        op=_integer_value(payload.get("op")),
        sequence=_integer_value(payload.get("s")),
        event_type=_string_value(payload.get("t")),
        data=copy.deepcopy(payload.get("d")),
        transport="webhook",
        raw_payload=raw_payload,
    )


def _integer_value(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _string_value(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value
