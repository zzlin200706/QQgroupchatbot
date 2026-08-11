import json

import httpx
import pytest

from app.adapters.qq_official.auth import QQAccessToken
from app.adapters.qq_official.message_api import (
    QQOfficialMessageAPIAuthenticationError,
    QQOfficialGroupMessageSendResult,
    QQOfficialGroupMessageSender,
    QQOfficialMessageAPINotFoundError,
    QQOfficialMessageAPIPermissionError,
    QQOfficialMessageAPIRateLimitError,
    QQOfficialMessageAPIResponseError,
    QQOfficialMessageAPIServerError,
    QQOfficialMessageAPITimeoutError,
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
async def test_explicit_msg_seq_is_forwarded_when_provided() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "content": "pong",
            "msg_type": 0,
            "msg_id": "message-id-1",
            "msg_seq": 3,
        }
        return httpx.Response(200, json={"id": "reply-id-3", "timestamp": 123})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=FakeAuthClient(),
            http_client=client,
        )
        result = await sender.send_group_message(
            "group-openid",
            "pong",
            msg_id="message-id-1",
            msg_seq=3,
        )

    assert result == QQOfficialGroupMessageSendResult(
        message_id="reply-id-3",
        timestamp=123,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"msg_id": None}, "msg_id"),
        ({"msg_id": "message-id-1", "msg_seq": 0}, "msg_seq"),
    ],
)
async def test_validates_passive_reply_requirements(
    kwargs: dict[str, object],
    match: str,
) -> None:
    sender = QQOfficialGroupMessageSender(auth_client=FakeAuthClient())

    with pytest.raises(ValueError, match=match):
        await sender.send_group_message("group-openid", "summary text", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, QQOfficialMessageAPIAuthenticationError),
        (403, QQOfficialMessageAPIPermissionError),
        (404, QQOfficialMessageAPINotFoundError),
        (429, QQOfficialMessageAPIRateLimitError),
        (503, QQOfficialMessageAPIServerError),
    ],
)
async def test_http_failures_are_mapped_by_status(
    status: int,
    error_type: type[Exception],
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={"code": 22009, "message": "msg limit exceed"},
            )
        )
    ) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=FakeAuthClient(),
            http_client=client,
        )
        with pytest.raises(error_type) as caught:
            await sender.send_group_message(
                "group-openid",
                "summary text",
                msg_id="message-id-1",
            )

    assert "test-access-token" not in str(caught.value)


@pytest.mark.asyncio
async def test_transport_timeout_and_response_failures_are_explicit() -> None:
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

    def timeout_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout_error)
    ) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=FakeAuthClient(),
            http_client=client,
        )
        with pytest.raises(QQOfficialMessageAPITimeoutError):
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

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"nope"))
    ) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=FakeAuthClient(),
            http_client=client,
        )
        with pytest.raises(QQOfficialMessageAPIResponseError, match="valid JSON"):
            await sender.send_group_message(
                "group-openid",
                "summary text",
                msg_id="message-id-1",
            )


@pytest.mark.asyncio
async def test_structured_http_error_redacts_access_token_and_authorization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "code": 22009,
                "message": (
                    "msg limit exceed QQBot test-access-token "
                    'access_token=test-access-token token=test-access-token'
                ),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=FakeAuthClient(),
            http_client=client,
        )
        with pytest.raises(QQOfficialMessageAPIRateLimitError) as caught:
            await sender.send_group_message(
                "group-openid",
                "summary text",
                msg_id="message-id-1",
            )

    text = str(caught.value)
    assert "test-access-token" not in text
    assert "QQBot <REDACTED>" in text
