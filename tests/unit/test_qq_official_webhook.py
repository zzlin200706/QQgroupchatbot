from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.adapters.qq_official.webhook import (
    QQOfficialWebhookAdapter,
    QQOfficialWebhookPayloadError,
    QQOfficialWebhookSignatureError,
)


def _seed(secret: str) -> bytes:
    seed = secret.encode("utf-8")
    while len(seed) < 32:
        seed = seed * 2
    return seed[:32]


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(_seed(secret))
    return private_key.sign(timestamp.encode("utf-8") + body).hex()


def _headers(secret: str, timestamp: str, body: bytes) -> dict[str, str]:
    return {
        "X-Signature-Ed25519": _signature(secret, timestamp, body),
        "X-Signature-Timestamp": timestamp,
        "X-Bot-Appid": "11111111",
    }


def test_callback_validation_matches_official_example() -> None:
    body = (
        b'{"d":{"plain_token":"Arq0D5A61EgUu4OxUvOp","event_ts":"1725442341"},"op":13}'
    )
    adapter = QQOfficialWebhookAdapter(
        bot_secret="DG5g3B4j9X2KOErG",
        app_id="11111111",
    )

    result = adapter.parse_request(
        headers={"X-Bot-Appid": "11111111"},
        body=body,
    )

    assert result.event is None
    assert result.validation_response is not None
    assert result.validation_response.plain_token == "Arq0D5A61EgUu4OxUvOp"
    assert (
        result.validation_response.signature
        == "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706"
    )


def test_verified_business_event_preserves_envelope_fields() -> None:
    payload = {
        "id": "event-1",
        "op": 0,
        "s": 9,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": "message-1",
            "content": "#ping",
            "group_openid": "group-1",
            "message_type": 0,
            "timestamp": "2026-08-11T12:00:00+08:00",
            "author": {"id": "user-1", "member_openid": "user-1"},
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    adapter = QQOfficialWebhookAdapter(
        bot_secret="test-app-secret",
        app_id="11111111",
    )

    result = adapter.parse_request(
        headers=_headers("test-app-secret", "1725442341", body),
        body=body,
    )

    assert result.validation_response is None
    assert result.event is not None
    assert result.event.transport == "webhook"
    assert result.event.event_id == "event-1"
    assert result.event.op == 0
    assert result.event.sequence == 9
    assert result.event.event_type == "GROUP_MESSAGE_CREATE"
    assert result.event.data == payload["d"]
    assert result.event.raw_payload == payload


def test_invalid_signature_is_rejected() -> None:
    body = b'{"id":"event-1","op":0,"s":1,"t":"GROUP_MESSAGE_CREATE","d":{}}'
    adapter = QQOfficialWebhookAdapter(bot_secret="test-app-secret")

    with pytest.raises(QQOfficialWebhookSignatureError, match="verification failed"):
        adapter.parse_request(
            headers={
                "X-Signature-Ed25519": "00" * 64,
                "X-Signature-Timestamp": "1725442341",
            },
            body=body,
        )


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Signature-Timestamp": "1725442341"},
        {"X-Signature-Ed25519": "ab"},
        {
            "X-Signature-Ed25519": "not-hex",
            "X-Signature-Timestamp": "1725442341",
        },
    ],
)
def test_missing_or_malformed_signature_headers_are_rejected(
    headers: dict[str, str],
) -> None:
    body = b'{"id":"event-1","op":0,"s":1,"t":"GROUP_MESSAGE_CREATE","d":{}}'
    adapter = QQOfficialWebhookAdapter(bot_secret="test-app-secret")

    with pytest.raises(QQOfficialWebhookSignatureError):
        adapter.parse_request(headers=headers, body=body)


def test_invalid_json_is_rejected_before_processing() -> None:
    adapter = QQOfficialWebhookAdapter(bot_secret="test-app-secret")

    with pytest.raises(QQOfficialWebhookPayloadError, match="valid JSON"):
        adapter.parse_request(headers={}, body=b"{not-json")


def test_validation_payload_requires_plain_token_and_event_ts() -> None:
    adapter = QQOfficialWebhookAdapter(bot_secret="test-app-secret")
    body = b'{"op":13,"d":{"plain_token":"only-token"}}'

    with pytest.raises(QQOfficialWebhookPayloadError, match="event_ts"):
        adapter.parse_request(headers={}, body=body)


def test_secret_never_appears_in_signature_errors() -> None:
    adapter = QQOfficialWebhookAdapter(bot_secret="super-secret-value")
    body = b'{"id":"event-1","op":0,"s":1,"t":"GROUP_MESSAGE_CREATE","d":{}}'

    with pytest.raises(QQOfficialWebhookSignatureError) as error:
        adapter.parse_request(headers={}, body=body)

    assert "super-secret-value" not in str(error.value)
