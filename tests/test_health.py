from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

import app.main as main_module
from app.config import Settings
from app.llm import LLMRequest, LLMResponse
from app.adapters.qq_official.inbound import QQOfficialInboundEvent
from app.storage.models import RawEvent


def runtime_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        _env_file=None,
        app_env="test",
        qq_event_transport="webhook",
        qq_bot_app_id="test-app-id",
        qq_bot_app_secret="test-app-secret",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}",
        summary_command_enabled=False,
        group_assistant_enabled=False,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_health_reports_application_status() -> None:
    # ``/health`` returns ``starting`` outside the lifespan; the
    # database probe is skipped because no engine is wired.
    app = main_module.create_app(Settings(_env_file=None, app_env="test"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"starting", "ready", "degraded", "stopping"}
    assert body["app"] == "qqgroupchatbot"
    assert body["environment"] == "test"
    assert isinstance(body["checks"], dict)


@pytest.mark.asyncio
async def test_health_reports_ready_inside_lifespan(tmp_path) -> None:
    app = main_module.create_app(runtime_settings(tmp_path))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["qq_adapter"] == "ready"
    assert body["checks"]["outbox_worker"] == "ready"


@pytest.mark.asyncio
async def test_health_reports_stopping_during_shutdown(tmp_path) -> None:
    app = main_module.create_app(runtime_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app) as state:
            pass
        # The lifespan context manager ran the ``finally`` branch;
        # the next probe must show ``stopping``.
        response = await client.get("/health")
    assert response.json()["status"] == "stopping"


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
            _env_file=None,
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
            # app_env="test" makes the Settings validator skip the
            # credential check, so the test can drive the lifecycle
            # purely through the patched create_llm_provider.
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


@pytest.mark.asyncio
async def test_ping_is_consumed_when_all_ai_features_are_disabled(tmp_path, monkeypatch):
    sent = asyncio.Event()
    calls = []

    class Sender:
        def __init__(self, **kwargs):
            pass

        async def send_group_message(self, group_id, message, *, msg_id=None):
            calls.append((group_id, message, msg_id))
            sent.set()
            return SimpleNamespace(message_id="pong-1")

    def unexpected_provider(*args, **kwargs):
        raise AssertionError("disabled AI must not construct a provider")

    monkeypatch.setattr(main_module, "QQOfficialGroupMessageSender", Sender)
    monkeypatch.setattr(main_module, "create_llm_provider", unexpected_provider)
    monkeypatch.setattr(main_module, "create_vision_provider", unexpected_provider)
    monkeypatch.setattr(main_module, "HttpImageResourceResolver", unexpected_provider)
    app = main_module.create_app(runtime_settings(tmp_path, vision_enabled=False))
    data = {
        "id": "ping-1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "group_openid": "group-1",
        "author": {"member_openid": "user-1"},
        "message_type": 0,
        "content": "#ping",
    }
    event = QQOfficialInboundEvent(
        event_id="event-ping-1", op=0, sequence=1,
        event_type="GROUP_MESSAGE_CREATE", data=data, transport="webhook",
        raw_payload={"op": 0, "t": "GROUP_MESSAGE_CREATE", "d": data},
    )
    async with app.router.lifespan_context(app):
        # Even explicit AI triggers remain ingestion-only when all features
        # are disabled. The image must not be fetched or sent to a model.
        for index, (event_type, content) in enumerate((
            ("GROUP_MESSAGE_CREATE", "入库验收测试"),
            ("GROUP_MESSAGE_CREATE", "#总结"),
            ("GROUP_MESSAGE_CREATE", "#问 测试问题"),
            ("GROUP_AT_MESSAGE_CREATE", "请描述图片"),
        ), start=2):
            passive_data = dict(data, id=f"stored-{index}", content=content)
            if event_type == "GROUP_AT_MESSAGE_CREATE":
                passive_data["attachments"] = [{
                    "content_type": "image/png",
                    "url": "https://media.example.test/private-test.png",
                }]
            passive_event = QQOfficialInboundEvent(
                event_id=f"event-{index}", op=0, sequence=index,
                event_type=event_type, data=passive_data, transport="webhook",
                raw_payload={"op": 0, "t": event_type, "d": passive_data},
            )
            passive_result = await app.state.qq_event_processor.process(passive_event)
            assert passive_result.raw_persisted and passive_result.normalized_persisted
            assert passive_result.interaction_name is None
            assert passive_result.outbox_id is None
        assert calls == []
        result = await app.state.qq_event_processor.process(event)
        assert result.raw_persisted and result.normalized_persisted
        await asyncio.wait_for(sent.wait(), timeout=3)
        assert calls == [("group-1", "pong", "ping-1")]
        assert app.state.llm_provider is None
        assert app.state.vision_provider is None
        assert app.state.image_enrichment_service is None
        assert len(await app.state.raw_event_repository.list_recent()) == 5


@pytest.mark.asyncio
async def test_startup_preserves_old_unparsed_raw_even_with_legacy_retention(tmp_path):
    settings = runtime_settings(tmp_path, raw_events_retention_days=1)
    app = main_module.create_app(settings)
    async with app.router.lifespan_context(app):
        raw = await app.state.raw_event_repository.insert(RawEvent(
            platform="qq_official",
            received_at=datetime.now(timezone.utc) - timedelta(days=365),
            raw_payload={"unparsed": True}, payload_hash="old-unparsed",
        ))
        raw_id = raw.id
    restarted = main_module.create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        stored = await restarted.state.raw_event_repository.get_by_id(raw_id)
        assert stored is not None
        assert stored.raw_payload == {"unparsed": True}


@pytest.mark.asyncio
async def test_health_detects_dead_worker(tmp_path):
    app = main_module.create_app(runtime_settings(tmp_path))
    async with app.router.lifespan_context(app):
        await app.state.qq_outbox_worker.stop()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")
        assert response.json()["status"] == "degraded"
        assert response.json()["checks"]["outbox_worker"] == "degraded"


@pytest.mark.asyncio
async def test_health_tracks_gateway_connection_failure_and_recovery(tmp_path, monkeypatch):
    reconnect = asyncio.Event()
    connected = asyncio.Event()

    class Gateway:
        def __init__(self, **kwargs):
            self.gateway_info = SimpleNamespace(session_start_limit=None)

        async def connect(self):
            if not reconnect.is_set():
                raise main_module.QQOfficialGatewayError("test connection failure")
            connected.set()

        async def next_event(self):
            await asyncio.Event().wait()

        async def close(self):
            pass

    monkeypatch.setattr(main_module, "QQOfficialGatewayClient", Gateway)
    app = main_module.create_app(runtime_settings(
        tmp_path, qq_event_transport="websocket",
        qq_gateway_reconnect_initial_delay_seconds=0.05,
        qq_gateway_reconnect_max_delay_seconds=0.05,
    ))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await asyncio.sleep(0.01)
            response = await client.get("/health")
            assert response.json()["status"] == "degraded"
            assert response.json()["checks"]["qq_adapter"] == "degraded"
            reconnect.set()
            await asyncio.wait_for(connected.wait(), timeout=1)
            response = await client.get("/health")
            assert response.json()["status"] == "ready"
            assert response.json()["checks"]["qq_adapter"] == "ready"
