from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app.main as main_module
from app.config import Settings
from app.llm import LLMRequest, LLMResponse


APP_ID = "11111111"
APP_SECRET = "test-app-secret"


def _seed(secret: str) -> bytes:
    seed = secret.encode("utf-8")
    while len(seed) < 32:
        seed = seed * 2
    return seed[:32]


def _headers(secret: str, timestamp: str, body: bytes) -> dict[str, str]:
    private_key = Ed25519PrivateKey.from_private_bytes(_seed(secret))
    return {
        "X-Bot-Appid": APP_ID,
        "X-Signature-Timestamp": timestamp,
        "X-Signature-Ed25519": private_key.sign(
            timestamp.encode("utf-8") + body
        ).hex(),
    }


def _body(
    *,
    event_id: str,
    sequence: int,
    message_id: str,
    text: str,
    group_openid: str,
    author_id: str,
    timestamp: str,
    event_type: str = "GROUP_MESSAGE_CREATE",
) -> bytes:
    payload = {
        "id": event_id,
        "op": 0,
        "s": sequence,
        "t": event_type,
        "d": {
            "id": message_id,
            "content": text,
            "group_openid": group_openid,
            "group_id": group_openid,
            "message_type": 0,
            "timestamp": timestamp,
            "author": {
                "id": author_id,
                "member_openid": author_id,
                "username": "测试用户",
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class FakeSender:
    def __init__(self, **_: object) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def send_group_message(
        self,
        group_id: str,
        message: str,
        *,
        msg_id: str | None = None,
    ) -> object:
        self.calls.append((group_id, message, msg_id))
        return object()


class FakeLLMProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content=json.dumps(
                {
                    "summary": "群内确认了测试安排。",
                    "topics": ["测试安排"],
                    "key_points": ["按计划执行"],
                    "decisions": [],
                    "action_items": [],
                    "open_questions": [],
                },
                ensure_ascii=False,
            ),
            provider="fake-provider",
            model="fake-model",
            finish_reason="stop",
            usage=None,
        )

    async def aclose(self) -> None:
        return None


async def _post_event(
    client: httpx.AsyncClient,
    *,
    secret: str,
    timestamp: str,
    body: bytes,
) -> httpx.Response:
    return await client.post(
        "/qq-official/webhook",
        content=body,
        headers=_headers(secret, timestamp, body),
    )


@pytest.mark.asyncio
async def test_webhook_ordinary_message_persists_raw_and_normalized_without_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "QQOfficialGroupMessageSender", FakeSender)
    app = main_module.create_app(
        Settings(
            app_env="test",
            qq_event_transport="webhook",
            qq_bot_app_id=APP_ID,
            qq_bot_app_secret=APP_SECRET,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'ordinary.db'}",
        )
    )
    body = _body(
        event_id="event-ordinary",
        sequence=1,
        message_id="message-ordinary",
        text="普通消息",
        group_openid="group-a",
        author_id="user-a",
        timestamp="2026-08-11T10:00:00+08:00",
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await _post_event(
                client,
                secret=APP_SECRET,
                timestamp="1725442341",
                body=body,
            )
        await app.state.qq_event_processor.drain()
        raw_events = await app.state.raw_event_repository.list_recent()
        assert len(raw_events) == 1
        normalized = await app.state.message_repository.get_by_raw_event_id(
            raw_events[0].id,
            parser_name="qq_official_message_parser",
            parser_version="1",
        )
        assert normalized is not None
        assert normalized.author.user_id == "user-a"
        assert normalized.context.group_id == "group-a"
        assert normalized.author.user_id != normalized.context.group_id
        assert app.state.qq_group_message_sender.calls == []

    assert response.status_code == 200
    assert response.json() == {"op": 12}


@pytest.mark.asyncio
async def test_webhook_ping_replies_without_calling_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "QQOfficialGroupMessageSender", FakeSender)
    app = main_module.create_app(
        Settings(
            app_env="test",
            qq_event_transport="webhook",
            qq_bot_app_id=APP_ID,
            qq_bot_app_secret=APP_SECRET,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'ping.db'}",
        )
    )
    body = _body(
        event_id="event-ping",
        sequence=2,
        message_id="message-ping",
        text="#ping",
        group_openid="group-a",
        author_id="user-a",
        timestamp="2026-08-11T10:05:00+08:00",
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await _post_event(
                client,
                secret=APP_SECRET,
                timestamp="1725442342",
                body=body,
            )
        await app.state.qq_event_processor.drain()
        raw_events = await app.state.raw_event_repository.list_recent()
        normalized = await app.state.message_repository.get_by_raw_event_id(
            raw_events[0].id,
            parser_name="qq_official_message_parser",
            parser_version="1",
        )
        assert normalized is not None
        assert normalized.author.user_id == "user-a"
        assert normalized.context.group_id == "group-a"
        assert app.state.qq_group_message_sender.calls == [
            ("group-a", "pong", "message-ping")
        ]

    assert response.status_code == 200
    assert response.json() == {"op": 12}


@pytest.mark.asyncio
async def test_webhook_summary_queries_current_group_only_and_replies_to_trigger_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeLLMProvider()
    monkeypatch.setattr(main_module, "QQOfficialGroupMessageSender", FakeSender)
    monkeypatch.setattr(main_module, "create_llm_provider", lambda settings: provider)
    app = main_module.create_app(
        Settings(
            app_env="test",
            qq_event_transport="webhook",
            qq_bot_app_id=APP_ID,
            qq_bot_app_secret=APP_SECRET,
            summary_command_enabled=True,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'summary.db'}",
        )
    )
    history_a = _body(
        event_id="event-a",
        sequence=3,
        message_id="message-a",
        text="A组消息：明天按计划进行测试。",
        group_openid="group-a",
        author_id="member-a",
        timestamp="2026-08-11T10:00:00+08:00",
    )
    history_b = _body(
        event_id="event-b",
        sequence=4,
        message_id="message-b",
        text="B组消息：这条不应该出现在A组总结里。",
        group_openid="group-b",
        author_id="member-b",
        timestamp="2026-08-11T10:30:00+08:00",
    )
    command = _body(
        event_id="event-summary",
        sequence=5,
        message_id="message-summary",
        text=" #总结 ",
        group_openid="group-a",
        author_id="member-a",
        timestamp="2026-08-11T11:00:00+08:00",
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response_a = await _post_event(
                client,
                secret=APP_SECRET,
                timestamp="1725442343",
                body=history_a,
            )
            response_b = await _post_event(
                client,
                secret=APP_SECRET,
                timestamp="1725442344",
                body=history_b,
            )
            response_command = await _post_event(
                client,
                secret=APP_SECRET,
                timestamp="1725442345",
                body=command,
            )
        await app.state.qq_event_processor.drain()
        history = await app.state.summary_repository.list_for_group(
            platform="qq_official",
            group_id="group-a",
        )
        assert len(history) == 1
        assert history[0].result.group_id == "group-a"
        assert len(provider.requests) == 1
        prompt = provider.requests[0].user_prompt
        assert "A组消息：明天按计划进行测试。" in prompt
        assert "B组消息：这条不应该出现在A组总结里。" not in prompt
        assert "#总结" not in prompt
        assert app.state.qq_group_message_sender.calls == [
            (
                "group-a",
                "【群聊总结】\n\n群内确认了测试安排。\n\n"
                "【主要话题】\n- 测试安排\n\n【关键内容】\n- 按计划执行",
                "message-summary",
            )
        ]

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert response_command.status_code == 200
    assert response_command.json() == {"op": 12}
