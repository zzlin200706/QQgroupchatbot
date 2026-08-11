"""QQ Official group-message sender backed by the current OpenAPI contract."""

from __future__ import annotations

import re
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

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        api_code: str | int | None = None,
        api_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code
        self.api_message = api_message


class QQOfficialMessageAPIAuthenticationError(QQOfficialMessageAPIHTTPError):
    """Outbound OpenAPI rejected authentication."""


class QQOfficialMessageAPIPermissionError(QQOfficialMessageAPIHTTPError):
    """Outbound OpenAPI rejected authorization or permission."""


class QQOfficialMessageAPINotFoundError(QQOfficialMessageAPIHTTPError):
    """Outbound OpenAPI did not find the target resource."""


class QQOfficialMessageAPIRateLimitError(QQOfficialMessageAPIHTTPError):
    """Outbound OpenAPI rejected the request due to frequency limiting."""


class QQOfficialMessageAPIServerError(QQOfficialMessageAPIHTTPError):
    """Outbound OpenAPI returned a server-side failure."""


class QQOfficialMessageAPIRequestError(QQOfficialMessageAPIHTTPError):
    """Outbound OpenAPI rejected a malformed or invalid request."""


class QQOfficialMessageAPITransportError(QQOfficialMessageAPIError):
    """Outbound OpenAPI could not be reached."""


class QQOfficialMessageAPITimeoutError(QQOfficialMessageAPITransportError):
    """Outbound OpenAPI timed out."""


class QQOfficialMessageAPIResponseError(QQOfficialMessageAPIError):
    """Outbound OpenAPI returned an invalid success payload."""


class AccessTokenProvider(Protocol):
    async def fetch_access_token(self) -> QQAccessToken: ...


@dataclass(frozen=True)
class QQOfficialGroupMessageSendResult:
    message_id: str
    timestamp: int | None


@dataclass(frozen=True)
class QQOfficialMessageAPIErrorDetail:
    code: str | int | None
    message: str | None


_QQBOT_AUTH_PATTERN = re.compile(r"(?i)\bQQBot\s+\S+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r'(?i)("?(?:access_token|authorization|token|client_secret|app_secret)"?\s*[:=]\s*"?)([^",\s]+)'
)


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
        msg_seq: int | None = None,
    ) -> QQOfficialGroupMessageSendResult:
        if not group_id.strip():
            raise ValueError("group_id must not be empty")
        if not message:
            raise ValueError("message must not be empty")
        if msg_id is None or not msg_id.strip():
            raise ValueError("msg_id is required for QQ Official group passive replies")
        if msg_seq is not None and (
            isinstance(msg_seq, bool) or not isinstance(msg_seq, int) or msg_seq < 1
        ):
            raise ValueError("msg_seq must be a positive integer when provided")

        access_token = await self._auth_client.fetch_access_token()
        payload = {
            "content": message,
            "msg_type": 0,
            "msg_id": msg_id,
            "msg_seq": 1 if msg_seq is None else msg_seq,
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
        except httpx.TimeoutException as error:
            raise QQOfficialMessageAPITimeoutError(
                "QQ Official group message request timed out"
            ) from error
        except httpx.RequestError as error:
            raise QQOfficialMessageAPITransportError(
                "QQ Official group message request failed"
            ) from error

        if not response.is_success:
            raise _http_error(response, access_token=access_token)

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


def _http_error(
    response: httpx.Response,
    *,
    access_token: str,
) -> QQOfficialMessageAPIHTTPError:
    detail = _error_detail(response, access_token=access_token)
    message = f"QQ Official group message request returned HTTP {response.status_code}"
    if detail.code is not None:
        message += f" code={detail.code}"
    if detail.message:
        message += f" message={detail.message}"

    if response.status_code == 401:
        error_type = QQOfficialMessageAPIAuthenticationError
    elif response.status_code == 403:
        error_type = QQOfficialMessageAPIPermissionError
    elif response.status_code == 404:
        error_type = QQOfficialMessageAPINotFoundError
    elif response.status_code == 429:
        error_type = QQOfficialMessageAPIRateLimitError
    elif response.status_code >= 500:
        error_type = QQOfficialMessageAPIServerError
    else:
        error_type = QQOfficialMessageAPIRequestError
    return error_type(
        message,
        status_code=response.status_code,
        api_code=detail.code,
        api_message=detail.message,
    )


def _error_detail(
    response: httpx.Response,
    *,
    access_token: str,
) -> QQOfficialMessageAPIErrorDetail:
    try:
        payload = response.json()
    except ValueError:
        return QQOfficialMessageAPIErrorDetail(code=None, message=None)
    if not isinstance(payload, Mapping):
        return QQOfficialMessageAPIErrorDetail(code=None, message=None)
    nested_error = payload.get("error")
    if isinstance(nested_error, Mapping):
        return QQOfficialMessageAPIErrorDetail(
            code=_error_code(payload.get("code") or nested_error.get("code")),
            message=_error_message(nested_error, access_token=access_token),
        )
    return QQOfficialMessageAPIErrorDetail(
        code=_error_code(payload.get("code")),
        message=_error_message(payload, access_token=access_token),
    )


def _error_code(value: object) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int)):
        return value
    return None


def _error_message(
    payload: Mapping[str, object],
    *,
    access_token: str,
) -> str | None:
    for candidate in (
        payload.get("message"),
        payload.get("msg"),
        payload.get("error"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return _sanitize(candidate, access_token=access_token)
    return None


def _sanitize(value: str, *, access_token: str) -> str:
    sanitized = _QQBOT_AUTH_PATTERN.sub("QQBot <REDACTED>", value)
    sanitized = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1<REDACTED>", sanitized)
    return sanitized.replace(access_token, "<REDACTED>")


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
