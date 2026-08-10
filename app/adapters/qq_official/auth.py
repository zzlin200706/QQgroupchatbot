"""Minimal AppID/AppSecret authentication for the QQ Official Bot API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import httpx


# QQ Official Bot documentation, updated 2026-07-21:
# https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/api-use.html
QQ_OFFICIAL_ACCESS_TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"


class QQOfficialAuthError(RuntimeError):
    """Base error for QQ Official Bot authentication failures."""


class QQOfficialAuthConfigurationError(QQOfficialAuthError):
    """Raised when an auth client is created without required credentials."""


class QQOfficialAuthTransportError(QQOfficialAuthError):
    """Raised when the token endpoint cannot be reached."""


class QQOfficialAuthHTTPError(QQOfficialAuthError):
    """Raised when the token endpoint returns a non-success HTTP status."""


class QQOfficialAuthResponseError(QQOfficialAuthError):
    """Raised when a successful response lacks a valid token shape."""


@dataclass(frozen=True)
class QQAccessToken:
    """An access token returned by the QQ Official Bot token endpoint."""

    access_token: str
    expires_in: int


class QQOfficialAuthClient:
    """Fetch a QQ Official Bot AccessToken without caching or refresh logic."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        timeout_seconds: float = 10.0,
        token_url: str = QQ_OFFICIAL_ACCESS_TOKEN_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not app_id.strip():
            raise QQOfficialAuthConfigurationError("QQ Official Bot AppID is required")
        if not app_secret.strip():
            raise QQOfficialAuthConfigurationError("QQ Official Bot AppSecret is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout_seconds = timeout_seconds
        self._token_url = token_url
        self._client = client

    async def fetch_access_token(self) -> QQAccessToken:
        """Request and validate one AccessToken from QQ's official endpoint."""

        if self._client is not None:
            return await self._fetch_with_client(self._client)

        timeout = httpx.Timeout(self._timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await self._fetch_with_client(client)

    async def _fetch_with_client(self, client: httpx.AsyncClient) -> QQAccessToken:
        try:
            response = await client.post(
                self._token_url,
                json={"appId": self._app_id, "clientSecret": self._app_secret},
                timeout=self._timeout_seconds,
            )
        except httpx.RequestError as error:
            raise QQOfficialAuthTransportError(
                "QQ Official Bot token request failed"
            ) from error

        if not response.is_success:
            raise QQOfficialAuthHTTPError(
                f"QQ Official Bot token request returned HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as error:
            raise QQOfficialAuthResponseError(
                "QQ Official Bot token response was not valid JSON"
            ) from error
        return _parse_access_token(payload)


def _parse_access_token(payload: object) -> QQAccessToken:
    if not isinstance(payload, Mapping):
        raise QQOfficialAuthResponseError(
            "QQ Official Bot token response must be a JSON object"
        )

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise QQOfficialAuthResponseError(
            "QQ Official Bot token response did not contain access_token"
        )

    expires_in = _positive_integer(payload.get("expires_in"))
    if expires_in is None:
        raise QQOfficialAuthResponseError(
            "QQ Official Bot token response did not contain a valid expires_in"
        )
    return QQAccessToken(access_token=access_token, expires_in=expires_in)


def _positive_integer(value: Any) -> int | None:
    """Accept the documented number and its documented string example."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if value.strip() == str(parsed) and parsed > 0 else None
    return None
