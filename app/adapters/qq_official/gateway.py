"""Minimal QQ Official Bot Gateway protocol client; no parsing or storage."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.adapters.qq_official.auth import QQAccessToken

logger = logging.getLogger(__name__)

QQ_OFFICIAL_OPENAPI_BASE_URL = "https://api.bot.qq.com"
QQ_OFFICIAL_GATEWAY_BOT_PATH = "/gateway/bot"
GROUP_AND_C2C_EVENT_INTENT = 1 << 25
GatewayEventHandler = Callable[["QQGatewayDispatch"], Awaitable[None] | None]


class QQOfficialGatewayError(RuntimeError):
    """Base error for QQ Official Gateway failures."""


class QQOfficialGatewayHTTPError(QQOfficialGatewayError):
    """Gateway OpenAPI returned a non-success HTTP status."""


class QQOfficialGatewayResponseError(QQOfficialGatewayError):
    """Gateway OpenAPI response had an invalid shape."""


class QQOfficialGatewayTransportError(QQOfficialGatewayError):
    """Gateway HTTP or WebSocket transport failed."""


class QQOfficialGatewayProtocolError(QQOfficialGatewayError):
    """Gateway did not follow the Phase C handshake protocol."""


class AccessTokenProvider(Protocol):
    async def fetch_access_token(self) -> QQAccessToken: ...


@dataclass(frozen=True)
class QQSessionStartLimit:
    total: int | None
    remaining: int | None
    reset_after: int | None
    max_concurrency: int | None


@dataclass(frozen=True)
class QQGatewayInfo:
    url: str
    shards: int | None
    session_start_limit: QQSessionStartLimit | None


@dataclass(frozen=True)
class QQGatewayDispatch:
    sequence: int | None
    event_type: str | None
    data: object


@dataclass(frozen=True)
class QQGatewayReady:
    session_id: str | None
    bot_id: str | None
    bot_username: str | None
    bot_flag: bool | None


def build_identify_payload(access_token: str) -> dict[str, object]:
    """Build documented Identify: QQBot AccessToken, intent, single shard."""

    if not access_token.strip():
        raise ValueError("access_token must not be empty")
    return {
        "op": 2,
        "d": {
            "token": f"QQBot {access_token}",
            "intents": GROUP_AND_C2C_EVENT_INTENT,
            "shard": [0, 1],
            "properties": {
                "$os": sys.platform,
                "$browser": "qqgroupchatbot",
                "$device": "qqgroupchatbot",
            },
        },
    }


def build_heartbeat_payload(sequence: int | None) -> dict[str, object]:
    return {"op": 1, "d": sequence}


def parse_gateway_info(payload: object) -> QQGatewayInfo:
    if not isinstance(payload, Mapping):
        raise QQOfficialGatewayResponseError("Gateway response must be a JSON object")
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith(("ws://", "wss://")):
        raise QQOfficialGatewayResponseError("Gateway response did not contain a WebSocket url")
    return QQGatewayInfo(
        url=url,
        shards=_integer(payload.get("shards")),
        session_start_limit=_session_limit(payload.get("session_start_limit")),
    )


def parse_hello_heartbeat_interval(payload: object) -> float:
    if not isinstance(payload, Mapping) or payload.get("op") != 10:
        raise QQOfficialGatewayProtocolError("Expected Gateway Hello opcode 10")
    data = payload.get("d")
    if not isinstance(data, Mapping):
        raise QQOfficialGatewayProtocolError("Gateway Hello data must be an object")
    interval = data.get("heartbeat_interval")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        raise QQOfficialGatewayProtocolError("Gateway Hello heartbeat_interval is invalid")
    return float(interval) / 1000


class QQOfficialGatewayClient:
    """Single-shard, no-resume client that delivers raw Gateway Dispatches."""

    def __init__(
        self,
        *,
        auth_client: AccessTokenProvider,
        event_handler: GatewayEventHandler | None = None,
        api_base_url: str = QQ_OFFICIAL_OPENAPI_BASE_URL,
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._auth_client = auth_client
        self._event_handler = event_handler
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._websocket: Any | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_interval_seconds: float | None = None
        self._latest_sequence: int | None = None
        self._last_heartbeat_ack = False
        self._gateway_info: QQGatewayInfo | None = None
        self._ready: QQGatewayReady | None = None

    @property
    def gateway_info(self) -> QQGatewayInfo | None:
        return self._gateway_info

    @property
    def ready(self) -> QQGatewayReady | None:
        return self._ready

    @property
    def latest_sequence(self) -> int | None:
        return self._latest_sequence

    @property
    def last_heartbeat_ack(self) -> bool:
        return self._last_heartbeat_ack

    async def connect(self) -> None:
        """Obtain URL then complete Hello → Identify → READY once."""

        access_token = await self._auth_client.fetch_access_token()
        gateway_info = await self._fetch_gateway_info(access_token.access_token)
        try:
            self._websocket = await websockets.connect(
                gateway_info.url, open_timeout=self._timeout_seconds, proxy=None
            )
        except (OSError, WebSocketException) as error:
            raise QQOfficialGatewayTransportError("QQ Official Gateway connection failed") from error
        self._gateway_info = gateway_info
        try:
            self._heartbeat_interval_seconds = parse_hello_heartbeat_interval(
                await self._receive_payload(timeout_seconds=self._timeout_seconds)
            )
            await self._send_payload(build_identify_payload(access_token.access_token))
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            await self._wait_for_ready()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            await websocket.close()

    async def next_event(self) -> QQGatewayDispatch:
        while True:
            dispatch = self.handle_payload(await self._receive_payload())
            if dispatch is not None:
                await self._dispatch_to_handler(dispatch)
                return dispatch

    def handle_payload(self, payload: object) -> QQGatewayDispatch | None:
        """Update protocol state without logging raw event data."""

        if not isinstance(payload, Mapping):
            raise QQOfficialGatewayProtocolError("Gateway payload must be a JSON object")
        if payload.get("op") == 11:
            self._last_heartbeat_ack = True
            return None
        if payload.get("op") != 0:
            return None
        sequence = _integer(payload.get("s"))
        if sequence is not None:
            self._latest_sequence = sequence
        event_type = payload.get("t")
        dispatch = QQGatewayDispatch(
            sequence=sequence,
            event_type=event_type if isinstance(event_type, str) else None,
            data=payload.get("d"),
        )
        if dispatch.event_type == "READY":
            self._ready = _ready(dispatch.data)
        logger.info("qq official gateway dispatch event_type=%s", dispatch.event_type)
        return dispatch

    async def _fetch_gateway_info(self, access_token: str) -> QQGatewayInfo:
        if self._http_client is not None:
            return await self._fetch_with_client(self._http_client, access_token)
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds)) as client:
            return await self._fetch_with_client(client, access_token)

    async def _fetch_with_client(self, client: httpx.AsyncClient, access_token: str) -> QQGatewayInfo:
        try:
            response = await client.get(
                f"{self._api_base_url}{QQ_OFFICIAL_GATEWAY_BOT_PATH}",
                headers={"Authorization": f"QQBot {access_token}"},
                timeout=self._timeout_seconds,
            )
        except httpx.RequestError as error:
            raise QQOfficialGatewayTransportError("QQ Official Gateway endpoint request failed") from error
        if not response.is_success:
            raise QQOfficialGatewayHTTPError(
                f"QQ Official Gateway endpoint returned HTTP {response.status_code}"
            )
        try:
            return parse_gateway_info(response.json())
        except ValueError as error:
            raise QQOfficialGatewayResponseError("Gateway endpoint returned invalid JSON") from error

    async def _wait_for_ready(self) -> None:
        while True:
            dispatch = self.handle_payload(
                await self._receive_payload(timeout_seconds=self._timeout_seconds)
            )
            if dispatch is not None and dispatch.event_type == "READY":
                return

    async def _heartbeat_loop(self) -> None:
        interval = self._heartbeat_interval_seconds
        if interval is None:
            return
        while True:
            await asyncio.sleep(interval)
            self._last_heartbeat_ack = False
            await self._send_payload(build_heartbeat_payload(self._latest_sequence))

    async def _receive_payload(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        if self._websocket is None:
            raise QQOfficialGatewayTransportError("QQ Official Gateway is not connected")
        try:
            receive = self._websocket.recv()
            raw = (
                await asyncio.wait_for(receive, timeout_seconds)
                if timeout_seconds is not None
                else await receive
            )
            payload = json.loads(raw)
        except asyncio.TimeoutError as error:
            raise QQOfficialGatewayTransportError(
                "QQ Official Gateway receive timed out"
            ) from error
        except (ConnectionClosed, OSError, WebSocketException) as error:
            raise QQOfficialGatewayTransportError("QQ Official Gateway connection closed") from error
        except (TypeError, json.JSONDecodeError) as error:
            raise QQOfficialGatewayProtocolError("QQ Official Gateway sent invalid JSON") from error
        if not isinstance(payload, dict):
            raise QQOfficialGatewayProtocolError("QQ Official Gateway sent non-object JSON")
        return payload

    async def _send_payload(self, payload: Mapping[str, object]) -> None:
        if self._websocket is None:
            raise QQOfficialGatewayTransportError("QQ Official Gateway is not connected")
        try:
            await asyncio.wait_for(
                self._websocket.send(json.dumps(payload, ensure_ascii=False)),
                self._timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            raise QQOfficialGatewayTransportError(
                "QQ Official Gateway send timed out"
            ) from error
        except (ConnectionClosed, OSError, WebSocketException) as error:
            raise QQOfficialGatewayTransportError("QQ Official Gateway connection closed") from error

    async def _dispatch_to_handler(self, dispatch: QQGatewayDispatch) -> None:
        if self._event_handler is None:
            return
        result = self._event_handler(dispatch)
        if inspect.isawaitable(result):
            await result


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _session_limit(value: object) -> QQSessionStartLimit | None:
    if not isinstance(value, Mapping):
        return None
    return QQSessionStartLimit(
        total=_integer(value.get("total")),
        remaining=_integer(value.get("remaining")),
        reset_after=_integer(value.get("reset_after")),
        max_concurrency=_integer(value.get("max_concurrency")),
    )


def _ready(value: object) -> QQGatewayReady:
    payload: Mapping[str, object] = value if isinstance(value, Mapping) else {}
    user = payload.get("user")
    user_payload: Mapping[str, object] = user if isinstance(user, Mapping) else {}
    session_id = payload.get("session_id")
    bot_id = user_payload.get("id")
    return QQGatewayReady(
        session_id=session_id if isinstance(session_id, str) else None,
        bot_id=bot_id if isinstance(bot_id, str) else None,
        bot_username=(
            user_payload.get("username")
            if isinstance(user_payload.get("username"), str)
            else None
        ),
        bot_flag=user_payload.get("bot") if isinstance(user_payload.get("bot"), bool) else None,
    )
