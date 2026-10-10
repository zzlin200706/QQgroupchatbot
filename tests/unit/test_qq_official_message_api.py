import json

import httpx
import pytest

from app.adapters.qq_official.auth import QQAccessToken
from app.adapters.qq_official.message_api import (
    QQOfficialMessageAPIAuthenticationError,
    QQOfficialMessageAPIBusinessError,
    QQOfficialGroupMessageSendResult,
    QQOfficialGroupMessageSender,
    QQOfficialMessageAPIHTTPError,
    QQOfficialMessageAPINotFoundError,
    QQOfficialMessageAPIPermissionError,
    QQOfficialMessageAPIRateLimitError,
    QQOfficialMessageAPIResponseError,
    QQOfficialMessageAPIServerError,
    QQOfficialMessageAPITimeoutError,
    QQOfficialMessageAPITransportError,
)


class FakeAuthClient:
    def __init__(self) -> None:
        self.invalidate_calls = 0

    async def fetch_access_token(self) -> QQAccessToken:
        return QQAccessToken(access_token="test-access-token", expires_in=7200)

    def invalidate(self) -> None:
        self.invalidate_calls += 1


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
    auth = FakeAuthClient()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={"code": 22009, "message": "msg limit exceed"},
            )
        )
    ) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=auth,
            http_client=client,
        )
        with pytest.raises(error_type) as caught:
            await sender.send_group_message(
                "group-openid",
                "summary text",
                msg_id="message-id-1",
            )

    assert "test-access-token" not in str(caught.value)
    if status == 401:
        assert auth.invalidate_calls == 1
    else:
        assert auth.invalidate_calls == 0


@pytest.mark.asyncio
async def test_401_response_invalidates_cached_token() -> None:
    auth = FakeAuthClient()
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(401, json={"code": 401, "message": "token bad"})
        return httpx.Response(200, json={"id": "ok", "timestamp": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = QQOfficialGroupMessageSender(
            auth_client=auth,
            http_client=client,
        )
        with pytest.raises(QQOfficialMessageAPIAuthenticationError):
            await sender.send_group_message(
                "group-openid", "hello", msg_id="m-1"
            )
        # Second call retries with a fresh (still mock) token.
        result = await sender.send_group_message(
            "group-openid", "hello", msg_id="m-2"
        )

    assert result.message_id == "ok"
    assert auth.invalidate_calls == 1
    assert call_count == 2


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


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201, 202])
@pytest.mark.parametrize(
    "error_fields",
    [
        {"err_code": 40034005},
        {"code": 40034005},
        {"code": 0, "error": {"code": 40034005}},
    ],
)
async def test_business_error_is_not_a_delivery_receipt_even_with_id(
    status: int, error_fields: dict,
) -> None:
    calls = []
    auth = FakeAuthClient()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"id": "not-delivered", **error_fields})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = QQOfficialGroupMessageSender(auth_client=auth, http_client=client)
        with pytest.raises(QQOfficialMessageAPIBusinessError) as caught:
            await sender.send_group_message("g", "pong", msg_id="m")

    assert caught.value.status_code == status
    assert caught.value.api_code == 40034005
    assert len(calls) == 1  # No retry of an uncertain outbound operation.
    assert auth.invalidate_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [201, 202])
async def test_async_acceptance_does_not_confirm_delivery(status: int) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"id": "pending", "err_code": 0}),
    )) as client:
        sender = QQOfficialGroupMessageSender(auth_client=FakeAuthClient(), http_client=client)
        with pytest.raises(QQOfficialMessageAPIResponseError, match="not confirm delivery"):
            await sender.send_group_message("g", "pong", msg_id="m")


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, "0"])
async def test_explicit_zero_business_code_allows_valid_receipt(code: int | str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id": "sent", "err_code": code}),
    )) as client:
        sender = QQOfficialGroupMessageSender(auth_client=FakeAuthClient(), http_client=client)
        result = await sender.send_group_message("g", "pong", msg_id="m")
    assert result.message_id == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [None, False, True, 0.0, "", [], {}])
async def test_malformed_business_code_is_not_silently_accepted(code: object) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id": "invalid", "err_code": code}),
    )) as client:
        sender = QQOfficialGroupMessageSender(auth_client=FakeAuthClient(), http_client=client)
        with pytest.raises(QQOfficialMessageAPIResponseError, match="error code"):
            await sender.send_group_message("g", "pong", msg_id="m")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
async def test_documented_error_code_and_message_are_preserved_and_redacted(status: int) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={
            "err_code": 40034005, "code": 0,
            "message": "expired QQBot test-access-token access_token=test-access-token",
        }),
    )) as client:
        sender = QQOfficialGroupMessageSender(auth_client=FakeAuthClient(), http_client=client)
        with pytest.raises(QQOfficialMessageAPIHTTPError) as caught:
            await sender.send_group_message("g", "pong", msg_id="m")
    assert caught.value.api_code == 40034005
    assert caught.value.api_message is not None
    assert "test-access-token" not in caught.value.api_message
    assert "test-access-token" not in str(caught.value)


@pytest.mark.asyncio
async def test_unexpected_string_error_code_cannot_leak_credentials() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            "err_code": "token=test-access-token", "id": "not-delivered",
        }),
    )) as client:
        sender = QQOfficialGroupMessageSender(auth_client=FakeAuthClient(), http_client=client)
        with pytest.raises(QQOfficialMessageAPIBusinessError) as caught:
            await sender.send_group_message("g", "pong", msg_id="m")
    assert caught.value.api_code == "token=<REDACTED>"
    assert "test-access-token" not in str(caught.value)
