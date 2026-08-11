import json

import httpx
import pytest

from app.adapters.qq_official.auth import QQAccessToken
from app.adapters.qq_official.message_api import (
    QQOfficialGroupMessageSendResult,
    QQOfficialGroupMessageSender,
    QQOfficialMessageAPIHTTPError,
    QQOfficialMessageAPIResponseError,
    QQOfficialMessageAPITransportError,
)


class FakeAuthClient:
    async def fetch_access_token(self) -> QQAccessToken:
        return QQAccessToken(access_token="test-access-token", expires_in=7200)


@pytest.mark.asyncio
async def test_sends_group_passive_reply_with_documented_endpoint_and_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert (
            str(request.url)
            == "https://api.bot.qq.com/v2/groups/group-openid/messages"
        )
        assert request.headers["authorization"] == "QQBot test-access-token"
        assert json.loads(request.content) == {
            "content": "summary text",
            "msg_type": 0,
            "msg_id": "message-id-1",
            "msg_seq": 1,
        }
        return httpx.Response(
            200,
            json={"id": "reply-id-1", "timestamp": 1_786_000_000},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=FakeAuthClient(),
            http_client=client,
        )
        result = await sender.send_group_message(
            "group-openid",
            "summary text",
            msg_id="message-id-1",
        )

    assert result == QQOfficialGroupMessageSendResult(
        message_id="reply-id-1",
        timestamp=1_786_000_000,
    )


@pytest.mark.asyncio
async def test_requires_msg_id_for_group_passive_reply() -> None:
    sender = QQOfficialGroupMessageSender(auth_client=FakeAuthClient())

    with pytest.raises(ValueError, match="msg_id"):
        await sender.send_group_message("group-openid", "summary text")


@pytest.mark.asyncio
async def test_http_transport_and_response_failures_are_explicit() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(429))
    ) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=FakeAuthClient(),
            http_client=client,
        )
        with pytest.raises(QQOfficialMessageAPIHTTPError, match="HTTP 429"):
            await sender.send_group_message(
                "group-openid",
                "summary text",
                msg_id="message-id-1",
            )

    def transport_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_error)
    ) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=FakeAuthClient(),
            http_client=client,
        )
        with pytest.raises(QQOfficialMessageAPITransportError):
            await sender.send_group_message(
                "group-openid",
                "summary text",
                msg_id="message-id-1",
            )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=FakeAuthClient(),
            http_client=client,
        )
        with pytest.raises(QQOfficialMessageAPIResponseError, match="contain id"):
            await sender.send_group_message(
                "group-openid",
                "summary text",
                msg_id="message-id-1",
            )
