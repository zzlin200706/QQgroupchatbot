import json
import logging

import httpx
import pytest

from app.adapters.qq_official.auth import QQAccessToken
from app.adapters.qq_official.gateway import (
    GROUP_AND_C2C_EVENT_INTENT,
    QQOfficialGatewayClient,
    QQOfficialGatewayResponseError,
    build_heartbeat_payload,
    build_identify_payload,
    parse_gateway_info,
    parse_hello_heartbeat_interval,
)


class FakeAuthClient:
    async def fetch_access_token(self) -> QQAccessToken:
        return QQAccessToken(access_token="test-access-token", expires_in=7200)


def test_parses_gateway_bot_response() -> None:
    info = parse_gateway_info(
        {
            "url": "wss://sandbox.example.invalid/websocket",
            "shards": 1,
            "session_start_limit": {"total": 1000, "remaining": 999, "reset_after": 1, "max_concurrency": 1},
        }
    )
    assert info.url == "wss://sandbox.example.invalid/websocket"
    assert info.shards == 1
    assert info.session_start_limit is not None
    assert info.session_start_limit.remaining == 999


@pytest.mark.parametrize("payload", [{}, {"url": ""}, {"url": "https://example.invalid"}])
def test_gateway_response_requires_websocket_url(payload: object) -> None:
    with pytest.raises(QQOfficialGatewayResponseError, match="url"):
        parse_gateway_info(payload)


def test_hello_identify_and_heartbeat_protocol_helpers() -> None:
    assert parse_hello_heartbeat_interval({"op": 10, "d": {"heartbeat_interval": 45000}}) == 45
    identify = build_identify_payload("access-token")
    assert identify["op"] == 2
    assert identify["d"]["token"] == "QQBot access-token"
    assert identify["d"]["intents"] == 1 << 25
    assert identify["d"]["intents"] == GROUP_AND_C2C_EVENT_INTENT
    assert identify["d"]["shard"] == [0, 1]
    assert build_heartbeat_payload(None) == {"op": 1, "d": None}
    assert build_heartbeat_payload(42) == {"op": 1, "d": 42}


def test_dispatch_updates_sequence_ready_ack_and_redacts_logs(caplog: pytest.LogCaptureFixture) -> None:
    client = QQOfficialGatewayClient(auth_client=FakeAuthClient())
    with caplog.at_level(logging.INFO):
        ready = client.handle_payload({"op": 0, "s": 12, "t": "READY", "d": {"session_id": "s", "user": {"id": "b"}}})
        event = client.handle_payload({"id": "event-13", "op": 0, "s": 13, "t": "GROUP_AT_MESSAGE_CREATE", "d": {"content": "private", "message_scene": {"ext": "secret"}}})
        assert client.handle_payload({"op": 11}) is None
    assert ready is not None and event is not None
    assert client.latest_sequence == 13
    assert client.ready is not None and client.ready.session_id == "s"
    assert client.last_heartbeat_ack is True
    assert event.event_id == "event-13"
    assert event.op == 0
    assert event.raw_payload == {
        "id": "event-13",
        "op": 0,
        "s": 13,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {"content": "private", "message_scene": {"ext": "secret"}},
    }
    assert "GROUP_AT_MESSAGE_CREATE" in caplog.text
    assert "private" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_gateway_openapi_uses_qqbot_access_token_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://api.bot.qq.com/gateway/bot"
        assert request.headers["authorization"] == "QQBot test-access-token"
        assert json.loads(request.content or b"null") is None
        return httpx.Response(200, json={"url": "wss://example.invalid/websocket"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = QQOfficialGatewayClient(auth_client=FakeAuthClient(), http_client=http_client)
        info = await client._fetch_gateway_info("test-access-token")
    assert info.url == "wss://example.invalid/websocket"
