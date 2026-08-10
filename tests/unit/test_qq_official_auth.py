import json
import logging

import httpx
import pytest

from app.adapters.qq_official.auth import (
    QQOfficialAuthClient,
    QQOfficialAuthConfigurationError,
    QQOfficialAuthHTTPError,
    QQOfficialAuthResponseError,
    QQOfficialAuthTransportError,
)


def mock_client(handler: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, base_url="https://example.invalid")


@pytest.mark.asyncio
async def test_fetches_access_token_with_documented_json_fields(caplog: pytest.LogCaptureFixture) -> None:
    secret = "test-app-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://api.bot.qq.com/app/getAppAccessToken"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "appId": "test-app-id",
            "clientSecret": secret,
        }
        return httpx.Response(200, json={"access_token": "test-token", "expires_in": "7200"})

    async with mock_client(httpx.MockTransport(handler)) as client:
        with caplog.at_level(logging.DEBUG):
            result = await QQOfficialAuthClient(
                app_id="test-app-id",
                app_secret=secret,
                client=client,
            ).fetch_access_token()

    assert result.access_token == "test-token"
    assert result.expires_in == 7200
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_http_failure_is_explicit() -> None:
    async with mock_client(httpx.MockTransport(lambda request: httpx.Response(401))) as client:
        auth = QQOfficialAuthClient(app_id="id", app_secret="secret", client=client)
        with pytest.raises(QQOfficialAuthHTTPError, match="HTTP 401"):
            await auth.fetch_access_token()


@pytest.mark.asyncio
async def test_transport_failure_is_explicit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    async with mock_client(httpx.MockTransport(handler)) as client:
        auth = QQOfficialAuthClient(app_id="id", app_secret="secret", client=client)
        with pytest.raises(QQOfficialAuthTransportError):
            await auth.fetch_access_token()


@pytest.mark.asyncio
async def test_invalid_json_and_missing_access_token_are_rejected() -> None:
    async with mock_client(
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"not-json"))
    ) as client:
        auth = QQOfficialAuthClient(app_id="id", app_secret="secret", client=client)
        with pytest.raises(QQOfficialAuthResponseError, match="valid JSON"):
            await auth.fetch_access_token()

    async with mock_client(httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as client:
        auth = QQOfficialAuthClient(app_id="id", app_secret="secret", client=client)
        with pytest.raises(QQOfficialAuthResponseError, match="access_token"):
            await auth.fetch_access_token()


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_in", [None, 0, -1, True, 1.5, "7200.0", "invalid"])
async def test_missing_or_invalid_expires_in_is_rejected(expires_in: object) -> None:
    async with mock_client(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"access_token": "test-token", "expires_in": expires_in},
            )
        )
    ) as client:
        auth = QQOfficialAuthClient(app_id="id", app_secret="secret", client=client)
        with pytest.raises(QQOfficialAuthResponseError, match="expires_in"):
            await auth.fetch_access_token()


def test_missing_app_id_or_app_secret_fails_before_network_access() -> None:
    with pytest.raises(QQOfficialAuthConfigurationError, match="AppID"):
        QQOfficialAuthClient(app_id="", app_secret="secret")
    with pytest.raises(QQOfficialAuthConfigurationError, match="AppSecret"):
        QQOfficialAuthClient(app_id="id", app_secret="")
