"""Async transport client for a OneBot 11 forward WebSocket server."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException


logger = logging.getLogger(__name__)

RawOneBotEvent = dict[str, object]
EventHandler = Callable[[RawOneBotEvent], Awaitable[None] | None]


class ConnectionState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


class OneBotClientError(RuntimeError):
    """Base error raised by the OneBot transport client."""


class OneBotNotConnectedError(OneBotClientError):
    """Raised when an action is attempted without an active connection."""


class OneBotActionTimeoutError(OneBotClientError):
    """Raised when an action response is not received before its timeout."""


class OneBotActionResponseError(OneBotClientError):
    """Raised when OneBot returns a non-success action response."""


class OneBotDisconnectedError(OneBotClientError):
    """Raised for actions interrupted by a WebSocket disconnection."""


@dataclass(frozen=True)
class OneBotSendResult:
    """Safe subset of a successful ``send_group_msg`` response."""

    message_id: str | None


class OneBotClient:
    """Maintain one universal (`/`) OneBot 11 forward WebSocket connection.

    `start()` launches the reconnecting receive loop. `stop()` intentionally ends
    it and fails any in-flight actions. `disconnect()` only closes the current
    socket; a running client reconnects automatically.
    """

    def __init__(
        self,
        *,
        url: str,
        access_token: str = "",
        event_handler: EventHandler | None = None,
        connect_timeout: float = 10.0,
        action_timeout: float = 10.0,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
    ) -> None:
        if reconnect_initial_delay <= 0:
            raise ValueError("reconnect_initial_delay must be greater than zero")
        if reconnect_max_delay < reconnect_initial_delay:
            raise ValueError(
                "reconnect_max_delay must be greater than or equal to "
                "reconnect_initial_delay"
            )

        self._url = url
        self._access_token = access_token
        self._event_handler = event_handler
        self._connect_timeout = connect_timeout
        self._action_timeout = action_timeout
        self._reconnect_initial_delay = reconnect_initial_delay
        self._reconnect_max_delay = reconnect_max_delay

        self._connection: Any | None = None
        self._connection_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._stop_requested = asyncio.Event()
        self._run_task: asyncio.Task[None] | None = None
        self._pending_actions: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._state = ConnectionState.STOPPED

    @property
    def state(self) -> ConnectionState:
        """Return the current transport state."""

        return self._state

    @property
    def is_connected(self) -> bool:
        """Return whether an active OneBot WebSocket is available."""

        return self._connected.is_set()

    async def start(self) -> None:
        """Start the background reconnecting receive loop."""

        if self._run_task and not self._run_task.done():
            return

        self._stop_requested.clear()
        self._run_task = asyncio.create_task(
            self._run_forever(),
            name="onebot-websocket-client",
        )

    async def stop(self) -> None:
        """Gracefully stop the receive loop and close the current socket."""

        self._stop_requested.set()
        await self.disconnect()

        task = self._run_task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._run_task = None
        self._state = ConnectionState.STOPPED
        self._fail_pending_actions(OneBotDisconnectedError("OneBot client stopped"))

    async def connect(self) -> None:
        """Establish the WebSocket once; reconnection is managed by `start()`."""

        async with self._connection_lock:
            if self._connection is not None:
                return

            self._state = ConnectionState.CONNECTING
            headers = None
            if self._access_token:
                headers = {"Authorization": f"Bearer {self._access_token}"}

            connection = await websockets.connect(
                self._url,
                additional_headers=headers,
                open_timeout=self._connect_timeout,
                proxy=None,
            )
            if self._stop_requested.is_set():
                await connection.close()
                return

            self._connection = connection
            self._connected.set()
            self._state = ConnectionState.CONNECTED
            logger.info("onebot connection established")

    async def disconnect(self) -> None:
        """Close the active socket without disabling automatic reconnect."""

        async with self._connection_lock:
            connection = self._connection
            self._connection = None
            self._connected.clear()

        if connection is not None:
            await connection.close()

    async def wait_until_connected(self, timeout: float | None = None) -> None:
        """Wait until a WebSocket connection is available."""

        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def call_action(
        self,
        action: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """Call one OneBot action and await the response with matching `echo`."""

        echo = uuid.uuid4().hex
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending_actions[echo] = future

        request: dict[str, object] = {"action": action, "echo": echo}
        if params:
            request["params"] = dict(params)

        try:
            async with self._send_lock:
                connection = self._connection
                if connection is None or not self.is_connected:
                    raise OneBotNotConnectedError("OneBot WebSocket is not connected")
                await connection.send(json.dumps(request, ensure_ascii=False))

            try:
                return await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=timeout if timeout is not None else self._action_timeout,
                )
            except asyncio.TimeoutError as error:
                raise OneBotActionTimeoutError(
                    f"OneBot action timed out: {action}"
                ) from error
        finally:
            self._pending_actions.pop(echo, None)

    async def get_message(self, message_id: str | int) -> dict[str, object]:
        """Retrieve one message through the documented OneBot `get_msg` action."""

        return await self.call_action("get_msg", {"message_id": message_id})

    async def get_forward_message(self, message_id: str) -> dict[str, object]:
        """Retrieve one forward bundle through NapCat's `get_forward_msg` action."""

        return await self.call_action("get_forward_msg", {"message_id": message_id})

    async def send_group_message(
        self,
        group_id: str,
        message: str,
    ) -> OneBotSendResult:
        """Send one text message to a group without retrying ambiguous failures."""

        if not group_id:
            raise ValueError("group_id must not be empty")
        if not message:
            raise ValueError("message must not be empty")
        response = await self.call_action(
            "send_group_msg",
            {"group_id": group_id, "message": message},
        )
        status = response.get("status")
        retcode = response.get("retcode")
        if (
            status != "ok"
            or not isinstance(retcode, int)
            or isinstance(retcode, bool)
            or retcode != 0
        ):
            raise OneBotActionResponseError(
                "OneBot send_group_msg returned a non-success response"
            )

        data = response.get("data")
        message_id: str | None = None
        if isinstance(data, Mapping):
            raw_message_id = data.get("message_id")
            if isinstance(raw_message_id, (str, int)) and not isinstance(
                raw_message_id, bool
            ):
                message_id = str(raw_message_id)
        return OneBotSendResult(message_id=message_id)

    async def _run_forever(self) -> None:
        delay = self._reconnect_initial_delay

        while not self._stop_requested.is_set():
            try:
                await self.connect()
                connection = self._connection
                if connection is not None:
                    delay = self._reconnect_initial_delay
                    await self._receive_loop(connection)
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException) as error:
                logger.warning(
                    "onebot connection lost or unavailable (%s)",
                    type(error).__name__,
                )
            finally:
                connection = self._connection
                await self._clear_connection(connection)
                self._fail_pending_actions(
                    OneBotDisconnectedError("OneBot WebSocket disconnected")
                )

            if not self._stop_requested.is_set():
                self._state = ConnectionState.RECONNECTING
                try:
                    await asyncio.wait_for(self._stop_requested.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    delay = min(delay * 2, self._reconnect_max_delay)

    async def _receive_loop(self, connection: Any) -> None:
        async for raw_message in connection:
            await self._handle_raw_message(raw_message)

    async def _handle_raw_message(self, raw_message: str | bytes) -> None:
        try:
            payload = json.loads(raw_message)
        except (TypeError, json.JSONDecodeError):
            logger.warning("onebot received non-JSON payload")
            return

        if not isinstance(payload, dict):
            logger.warning("onebot received JSON payload that is not an object")
            return

        event = payload
        echo = event.get("echo")
        if isinstance(echo, str):
            pending = self._pending_actions.get(echo)
            if pending is not None and not pending.done():
                pending.set_result(event)
                return

        if event.get("post_type") == "meta_event":
            self._log_meta_event(event)
            return

        await self._dispatch_event(event)

    async def _dispatch_event(self, event: RawOneBotEvent) -> None:
        if self._event_handler is None:
            return

        try:
            result = self._event_handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("onebot event handler failed")

    async def _clear_connection(self, connection: Any | None) -> None:
        async with self._connection_lock:
            if connection is not None and self._connection is not connection:
                return
            self._connection = None
            self._connected.clear()

    def _fail_pending_actions(self, error: OneBotClientError) -> None:
        for future in self._pending_actions.values():
            if not future.done():
                future.set_exception(error)

    @staticmethod
    def _log_meta_event(event: RawOneBotEvent) -> None:
        logger.debug(
            "onebot meta event ignored: %s",
            event.get("meta_event_type", "unknown"),
        )
