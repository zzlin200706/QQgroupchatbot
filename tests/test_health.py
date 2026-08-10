import httpx
import pytest
import websockets

from app.config import Settings
from app.main import create_app


@pytest.mark.asyncio
async def test_health_reports_application_status() -> None:
    app = create_app(Settings(app_env="test"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app": "qqgroupchatbot",
        "environment": "test",
    }


@pytest.mark.asyncio
async def test_lifespan_initializes_storage_and_stops_onebot_client(tmp_path) -> None:
    async def handler(connection: websockets.ServerConnection) -> None:
        await connection.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        app = create_app(
            Settings(
                app_env="test",
                onebot_ws_url=f"ws://127.0.0.1:{port}",
                database_url=f"sqlite+aiosqlite:///{tmp_path / 'lifespan.db'}",
                onebot_reconnect_initial_delay_seconds=0.01,
                onebot_reconnect_max_delay_seconds=0.02,
            )
        )

        async with app.router.lifespan_context(app):
            client = app.state.onebot_client
            await client.wait_until_connected(timeout=1)
            assert client.is_connected
            assert await app.state.raw_event_repository.list_recent() == []

        assert not client.is_connected
