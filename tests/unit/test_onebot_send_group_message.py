from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from app.adapters.onebot.client import (
    OneBotActionResponseError,
    OneBotActionTimeoutError,
    OneBotClient,
    OneBotSendResult,
)


@pytest.mark.asyncio
async def test_send_group_message_uses_action_echo_and_validates_success() -> None:
    requests: list[dict[str, object]] = []

    async def handler(connection: websockets.ServerConnection) -> None:
        request = json.loads(await connection.recv())
        requests.append(request)
        await connection.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": 12345},
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
        try:
            result = await client.send_group_message("synthetic-group", "safe text")
        finally:
            await client.stop()

    assert result == OneBotSendResult(message_id="12345")
    assert requests == [
        {
            "action": "send_group_msg",
            "params": {
                "group_id": "synthetic-group",
                "message": "safe text",
            },
            "echo": requests[0]["echo"],
        }
    ]


@pytest.mark.asyncio
async def test_send_group_message_rejects_non_success_response() -> None:
    async def handler(connection: websockets.ServerConnection) -> None:
        request = json.loads(await connection.recv())
        await connection.send(
            json.dumps(
                {
                    "status": "failed",
                    "retcode": 100,
                    "data": None,
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
        try:
            with pytest.raises(OneBotActionResponseError):
                await client.send_group_message("synthetic-group", "safe text")
        finally:
            await client.stop()


@pytest.mark.asyncio
async def test_send_group_message_timeout_is_not_retried() -> None:
    request_count = 0
    request_received = asyncio.Event()

    async def handler(connection: websockets.ServerConnection) -> None:
        nonlocal request_count
        await connection.recv()
        request_count += 1
        request_received.set()
        await connection.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OneBotClient(
            url=f"ws://127.0.0.1:{port}",
            action_timeout=0.03,
            reconnect_initial_delay=0.01,
            reconnect_max_delay=0.02,
        )
        await client.start()
        await client.wait_until_connected(timeout=1)
        try:
            with pytest.raises(OneBotActionTimeoutError):
                await client.send_group_message("synthetic-group", "safe text")
            await asyncio.wait_for(request_received.wait(), timeout=1)
        finally:
            await client.stop()

    assert request_count == 1
