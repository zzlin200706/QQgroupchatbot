from __future__ import annotations

from app.adapters.qq_official.gateway import QQGatewayDispatch
from app.adapters.qq_official.inbound import inbound_event_from_gateway_dispatch


def test_gateway_dispatch_adapts_to_transport_neutral_inbound_event() -> None:
    dispatch = QQGatewayDispatch(
        sequence=42,
        event_type="GROUP_MESSAGE_CREATE",
        data={"id": "message-1", "content": "hello"},
        event_id="event-1",
        op=0,
        raw_payload={
            "id": "event-1",
            "op": 0,
            "s": 42,
            "t": "GROUP_MESSAGE_CREATE",
            "d": {"id": "message-1", "content": "hello"},
        },
    )

    event = inbound_event_from_gateway_dispatch(dispatch)

    assert event.transport == "websocket"
    assert event.event_id == "event-1"
    assert event.op == 0
    assert event.sequence == 42
    assert event.event_type == "GROUP_MESSAGE_CREATE"
    assert event.data == {"id": "message-1", "content": "hello"}
    assert event.raw_payload == {
        "id": "event-1",
        "op": 0,
        "s": 42,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {"id": "message-1", "content": "hello"},
    }


def test_gateway_dispatch_without_raw_payload_is_synthesized_losslessly() -> None:
    dispatch = QQGatewayDispatch(
        sequence=7,
        event_type="GROUP_MESSAGE_CREATE",
        data={"id": "message-2"},
        event_id="event-2",
    )

    event = inbound_event_from_gateway_dispatch(dispatch)

    assert event.raw_payload == {
        "id": "event-2",
        "op": 0,
        "s": 7,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {"id": "message-2"},
    }
