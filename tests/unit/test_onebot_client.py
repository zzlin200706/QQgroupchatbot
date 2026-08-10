import asyncio
import json
from collections.abc import Callable

import pytest
import websockets

from app.adapters.onebot.client import (
    ConnectionState,
    OneBotActionTimeoutError,
    OneBotClient,
    OneBotDisconnectedError,
)


async def wait_for_event(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


async def wait_for_condition(predicate: Callable[[], bool]) -> None:
    async def condition_is_true() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(condition_is_true(), timeout=1)


@pytest.mark.asyncio
async def test_connects_with_bearer_token_and_correlates_action_response() -> None:
    authorization_headers: list[str | None] = []

    async def handler(connection: websockets.ServerConnection) -> None:
        authorization_headers.append(connection.request.headers.get("Authorization"))
        request = json.loads(await connection.recv())
        await connection.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"online": True},
                    "echo": request["echo"],
                }
            )
        )
        await connection.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OneBotClient(
            url=f"ws://127.0.0.1:{port}",
            access_token="test-token",
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
        )
        await client.start()
        await client.wait_until_connected(timeout=1)

        response = await client.call_action("get_status")

        assert response["status"] == "ok"
        assert response["data"] == {"online": True}
        assert authorization_headers == ["Bearer test-token"]
        await client.stop()


@pytest.mark.asyncio
async def test_correlates_concurrent_action_responses_by_echo() -> None:
    async def handler(connection: websockets.ServerConnection) -> None:
        first = json.loads(await connection.recv())
        second = json.loads(await connection.recv())
        for request in (second, first):
            await connection.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"action": request["action"]},
                        "echo": request["echo"],
                    }
                )
            )
        await connection.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OneBotClient(
            url=f"ws://127.0.0.1:{port}",
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
        )
        await client.start()
        await client.wait_until_connected(timeout=1)

        status, login = await asyncio.gather(
            client.call_action("get_status"),
            client.call_action("get_login_info"),
        )

        assert status["data"] == {"action": "get_status"}
        assert login["data"] == {"action": "get_login_info"}
        await client.stop()


@pytest.mark.asyncio
async def test_action_timeout_is_reported() -> None:
    received_action = asyncio.Event()

    async def handler(connection: websockets.ServerConnection) -> None:
        await connection.recv()
        received_action.set()
        await connection.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OneBotClient(
            url=f"ws://127.0.0.1:{port}",
            action_timeout=0.05,
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
        )
        await client.start()
        await client.wait_until_connected(timeout=1)

        with pytest.raises(OneBotActionTimeoutError):
            await client.call_action("get_status")

        await wait_for_event(received_action)
        await client.stop()


@pytest.mark.asyncio
async def test_pending_action_fails_when_remote_connection_closes() -> None:
    action_received = asyncio.Event()

    async def handler(connection: websockets.ServerConnection) -> None:
        await connection.recv()
        action_received.set()
        await connection.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OneBotClient(
            url=f"ws://127.0.0.1:{port}",
            action_timeout=1,
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
        )
        await client.start()
        await client.wait_until_connected(timeout=1)

        action = asyncio.create_task(client.call_action("get_status"))
        await wait_for_event(action_received)
        with pytest.raises(OneBotDisconnectedError):
            await action

        await client.stop()


@pytest.mark.asyncio
async def test_meta_events_are_not_dispatched_as_messages() -> None:
    received_events: list[dict[str, object]] = []
    group_event_received = asyncio.Event()

    async def event_handler(event: dict[str, object]) -> None:
        received_events.append(event)
        group_event_received.set()

    async def handler(connection: websockets.ServerConnection) -> None:
        await connection.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                }
            )
        )
        await connection.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "interval": 30000,
                }
            )
        )
        await connection.send(
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "message_id": 1001,
                    "group_id": 2002,
                }
            )
        )
        await connection.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OneBotClient(
            url=f"ws://127.0.0.1:{port}",
            event_handler=event_handler,
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
        )
        await client.start()
        await wait_for_event(group_event_received)

        assert received_events == [
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 1001,
                "group_id": 2002,
            }
        ]
        await client.stop()


@pytest.mark.asyncio
async def test_reconnects_after_remote_server_closes_connection() -> None:
    connection_count = 0
    reconnected = asyncio.Event()

    async def handler(connection: websockets.ServerConnection) -> None:
        nonlocal connection_count
        connection_count += 1
        if connection_count == 1:
            await connection.close()
            return
        reconnected.set()
        await connection.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OneBotClient(
            url=f"ws://127.0.0.1:{port}",
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
        )
        await client.start()
        await wait_for_event(reconnected)
        await wait_for_condition(
            lambda: client.state == ConnectionState.CONNECTED and client.is_connected
        )

        assert connection_count >= 2
        assert client.state == ConnectionState.CONNECTED
        await client.stop()
