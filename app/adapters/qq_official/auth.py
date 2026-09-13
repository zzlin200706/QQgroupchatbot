"""Minimal AppID/AppSecret authentication for the QQ Official Bot API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Mapping

import httpx


# QQ Official Bot documentation, checked 2026-08-11:
# https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html
QQ_OFFICIAL_ACCESS_TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"

# Default safety margin subtracted from the documented `expires_in` so we
# never use a token in the last few seconds of its lifetime.  The
# official docs do not specify exact refresh behaviour, so a one-minute
# margin is a conservative default for both the documented 7200s and
# any shorter server-side adjustments.
DEFAULT_TOKEN_EXPIRY_SAFETY_SECONDS = 60.0


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
    """Fetch a QQ Official Bot AccessToken with in-process caching.

    The endpoint allows short-lived caching: ``expires_in`` is honoured
    and a configurable safety margin is subtracted so we never use a
    token in the last few seconds of its declared lifetime.  The
    underlying HTTP client is reused across calls; the caller owns its
    lifecycle (``aclose`` is *not* called here).
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        timeout_seconds: float = 10.0,
        token_url: str = QQ_OFFICIAL_ACCESS_TOKEN_URL,
        client: httpx.AsyncClient | None = None,
        expiry_safety_seconds: float = DEFAULT_TOKEN_EXPIRY_SAFETY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not app_id.strip():
            raise QQOfficialAuthConfigurationError("QQ Official Bot AppID is required")
        if not app_secret.strip():
            raise QQOfficialAuthConfigurationError("QQ Official Bot AppSecret is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if expiry_safety_seconds < 0:
            raise ValueError("expiry_safety_seconds must not be negative")

        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout_seconds = timeout_seconds
        self._token_url = token_url
        self._client = client
        self._expiry_safety_seconds = expiry_safety_seconds
        self._clock = clock
        self._cached_token: QQAccessToken | None = None
        self._cached_token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def fetch_access_token(self) -> QQAccessToken:
        """Return a fresh or cached AccessToken, fetching when necessary."""

        if self._is_cached_token_fresh():
            return self._cached_token  # type: ignore[return-value]
        async with self._lock:
            # Re-check after acquiring the lock to avoid a duplicate
            # token request under concurrent callers.
            if self._is_cached_token_fresh():
                return self._cached_token  # type: ignore[return-value]
            token = await self._fetch_new_token()
            self._cached_token = token
            self._cached_token_expires_at = (
                self._clock() + token.expires_in - self._expiry_safety_seconds
            )
            return token

    def invalidate(self) -> None:
        """Drop the cached token; the next call will re-fetch."""

        self._cached_token = None
        self._cached_token_expires_at = 0.0

    def _is_cached_token_fresh(self) -> bool:
        if self._cached_token is None:
            return False
        return self._clock() < self._cached_token_expires_at

    async def _fetch_new_token(self) -> QQAccessToken:
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
