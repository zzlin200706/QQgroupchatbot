"""QQ Official group-message sender backed by the current OpenAPI contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

import httpx

from app.adapters.qq_official.auth import QQAccessToken
from app.adapters.qq_official.gateway import QQ_OFFICIAL_OPENAPI_BASE_URL


QQ_OFFICIAL_GROUP_MESSAGES_PATH = "/v2/groups/{group_openid}/messages"


class QQOfficialMessageAPIError(RuntimeError):
    """Base error for QQ Official outbound message API failures."""


class QQOfficialMessageAPIHTTPError(QQOfficialMessageAPIError):
    """Outbound OpenAPI returned a non-success HTTP status."""


class QQOfficialMessageAPITransportError(QQOfficialMessageAPIError):
    """Outbound OpenAPI could not be reached."""


class QQOfficialMessageAPIResponseError(QQOfficialMessageAPIError):
    """Outbound OpenAPI returned an invalid success payload."""


class AccessTokenProvider(Protocol):
    async def fetch_access_token(self) -> QQAccessToken: ...


@dataclass(frozen=True)
class QQOfficialGroupMessageSendResult:
    message_id: str
    timestamp: int | None


class QQOfficialGroupMessageSender:
    """Send one QQ Official group text message as a passive reply."""

    def __init__(
        self,
        *,
        auth_client: AccessTokenProvider,
        api_base_url: str = QQ_OFFICIAL_OPENAPI_BASE_URL,
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._auth_client = auth_client
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client

    async def send_group_message(
        self,
        group_id: str,
        message: str,
        *,
        msg_id: str | None = None,
    ) -> QQOfficialGroupMessageSendResult:
        if not group_id.strip():
            raise ValueError("group_id must not be empty")
        if not message:
            raise ValueError("message must not be empty")
        if msg_id is None or not msg_id.strip():
            raise ValueError("msg_id is required for QQ Official group passive replies")

        access_token = await self._auth_client.fetch_access_token()
        payload = {
            "content": message,
            "msg_type": 0,
            "msg_id": msg_id,
            "msg_seq": 1,
        }
        if self._http_client is not None:
            return await self._send_with_client(
                self._http_client,
                access_token.access_token,
                group_id,
                payload,
            )
        timeout = httpx.Timeout(self._timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await self._send_with_client(
                client,
                access_token.access_token,
                group_id,
                payload,
            )

    async def _send_with_client(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        group_id: str,
        payload: dict[str, object],
    ) -> QQOfficialGroupMessageSendResult:
        try:
            response = await client.post(
                (
                    f"{self._api_base_url}"
                    f"{QQ_OFFICIAL_GROUP_MESSAGES_PATH.format(group_openid=group_id)}"
                ),
                headers={"Authorization": f"QQBot {access_token}"},
                json=payload,
                timeout=self._timeout_seconds,
            )
        except httpx.RequestError as error:
            raise QQOfficialMessageAPITransportError(
                "QQ Official group message request failed"
            ) from error

        if not response.is_success:
            raise QQOfficialMessageAPIHTTPError(
                f"QQ Official group message request returned HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as error:
            raise QQOfficialMessageAPIResponseError(
                "QQ Official group message response was not valid JSON"
            ) from error
        return _parse_send_result(data)


def _parse_send_result(payload: object) -> QQOfficialGroupMessageSendResult:
    if not isinstance(payload, Mapping):
        raise QQOfficialMessageAPIResponseError(
            "QQ Official group message response must be a JSON object"
        )
    message_id = _non_empty_string(payload.get("id"))
    if message_id is None:
        raise QQOfficialMessageAPIResponseError(
            "QQ Official group message response did not contain id"
        )
    return QQOfficialGroupMessageSendResult(
        message_id=message_id,
        timestamp=_as_int(payload.get("timestamp")),
    )


def _non_empty_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
