from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest

import app.main as main_module
from app.config import Settings
from app.llm import LLMRequest, LLMResponse


@pytest.mark.asyncio
async def test_health_reports_application_status() -> None:
    app = main_module.create_app(Settings(app_env="test"))

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
async def test_lifespan_initializes_storage_and_stops_qq_gateway_client(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGatewayClient:
        def __init__(self, **kwargs) -> None:
            self.connected = False
            self.closed = False
            self.gateway_info = SimpleNamespace(session_start_limit=None)
            self.connect_calls = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            self.connected = True

        async def next_event(self):
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.connected = False
            self.closed = True

    monkeypatch.setattr(main_module, "QQOfficialGatewayClient", FakeGatewayClient)
    app = main_module.create_app(
        Settings(
            app_env="test",
            qq_bot_app_id="test-app-id",
            qq_bot_app_secret="test-app-secret",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'lifespan.db'}",
            qq_gateway_reconnect_initial_delay_seconds=0.01,
            qq_gateway_reconnect_max_delay_seconds=0.02,
        )
    )

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        client = app.state.qq_gateway_client
        assert client.connected is True
        assert client.connect_calls == 1
        assert await app.state.raw_event_repository.list_recent() == []
        assert app.state.qq_gateway_task.done() is False

    assert client.connected is False
    assert client.closed is True


def test_configure_logging_uses_settings_level(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    def fake_basic_config(*, level: int) -> None:
        calls["basicConfig"] = level

    monkeypatch.setattr(main_module.logging, "basicConfig", fake_basic_config)
    root.handlers[:] = []
    try:
        main_module._configure_logging("debug")
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    assert calls == {"basicConfig": logging.DEBUG}


@pytest.mark.asyncio
async def test_summary_and_assistant_share_one_provider_lifecycle(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProvider:
        def __init__(self) -> None:
            self.close_calls = 0

        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise AssertionError(f"provider should not be called: {request}")

        async def aclose(self) -> None:
            self.close_calls += 1

    provider = FakeProvider()
    create_calls: list[Settings] = []

    def create_provider(settings: Settings) -> FakeProvider:
        create_calls.append(settings)
        return provider

    monkeypatch.setattr(main_module, "create_llm_provider", create_provider)
    app = main_module.create_app(
        Settings(
            _env_file=None,
            app_env="test",
            qq_event_transport="webhook",
            qq_bot_app_id="test-app-id",
            qq_bot_app_secret="test-app-secret",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'shared-provider.db'}",
            summary_command_enabled=True,
            group_assistant_enabled=True,
        )
    )

    async with app.router.lifespan_context(app):
        assert app.state.llm_provider is provider
        assert app.state.summary_command_handler is not None
        assert app.state.group_assistant_handler is not None
        assert len(create_calls) == 1

    assert provider.close_calls == 1
