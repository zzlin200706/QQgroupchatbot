from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app.main as main_module
from app.services.qq_official_event_processor import QQOfficialEventProcessResult
from app.config import Settings


def _seed(secret: str) -> bytes:
    seed = secret.encode("utf-8")
    while len(seed) < 32:
        seed = seed * 2
    return seed[:32]


def _headers(secret: str, timestamp: str, body: bytes) -> dict[str, str]:
    private_key = Ed25519PrivateKey.from_private_bytes(_seed(secret))
    return {
        "X-Bot-Appid": "11111111",
        "X-Signature-Timestamp": timestamp,
        "X-Signature-Ed25519": private_key.sign(
            timestamp.encode("utf-8") + body
        ).hex(),
    }


class FakeProcessor:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def process(self, event: object) -> QQOfficialEventProcessResult:
        self.calls.append(event)
        return QQOfficialEventProcessResult(
            transport="webhook",
            event_type="GROUP_MESSAGE_CREATE",
            raw_persisted=True,
            normalized_persisted=True,
            raw_event_id=1,
            interaction_name=None,
        )


def webhook_app(tmp_path: Path, *, secret: str = "test-app-secret"):
    return main_module.create_app(
        Settings(
            app_env="test",
            qq_event_transport="webhook",
            qq_bot_app_id="11111111",
            qq_bot_app_secret=secret,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'webhook-endpoint.db'}",
        )
    )


@pytest.mark.asyncio
async def test_webhook_callback_validation_request_returns_documented_payload(
    tmp_path: Path,
) -> None:
    app = webhook_app(tmp_path, secret="DG5g3B4j9X2KOErG")
    body = (
        b'{"d":{"plain_token":"Arq0D5A61EgUu4OxUvOp","event_ts":"1725442341"},"op":13}'
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/qq-official/webhook",
                content=body,
                headers={"X-Bot-Appid": "11111111"},
            )

    assert response.status_code == 200
    assert response.json() == {
        "plain_token": "Arq0D5A61EgUu4OxUvOp",
        "signature": "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706",
    }


@pytest.mark.asyncio
async def test_webhook_valid_business_event_reaches_processor_exactly_once(
    tmp_path: Path,
) -> None:
    app = webhook_app(tmp_path)
    processor = FakeProcessor()
    payload = {
        "id": "event-1",
        "op": 0,
        "s": 3,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": "message-1",
            "content": "hello",
            "message_type": 0,
            "group_openid": "group-1",
            "timestamp": "2026-08-11T12:00:00+08:00",
            "author": {"id": "user-1", "member_openid": "user-1"},
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    async with app.router.lifespan_context(app):
        app.state.qq_event_processor = processor
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/qq-official/webhook",
                content=body,
                headers=_headers("test-app-secret", "1725442341", body),
            )

    assert response.status_code == 200
    assert response.json() == {"op": 12}
    assert len(processor.calls) == 1


@pytest.mark.asyncio
async def test_webhook_invalid_signature_is_rejected(tmp_path: Path) -> None:
    app = webhook_app(tmp_path)
    body = b'{"id":"event-1","op":0,"s":1,"t":"GROUP_MESSAGE_CREATE","d":{}}'

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/qq-official/webhook",
                content=body,
                headers={
                    "X-Bot-Appid": "11111111",
                    "X-Signature-Timestamp": "1725442341",
                    "X-Signature-Ed25519": "00" * 64,
                },
            )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid webhook signature"


@pytest.mark.asyncio
async def test_webhook_invalid_json_is_rejected(tmp_path: Path) -> None:
    app = webhook_app(tmp_path)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/qq-official/webhook",
                content=b"{not-json",
            )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid webhook payload"
