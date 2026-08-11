"""QQ Official webhook callback verification and event adaptation."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.adapters.qq_official.inbound import (
    QQOfficialInboundEvent,
    inbound_event_from_webhook_payload,
)


QQ_OFFICIAL_WEBHOOK_SIGNATURE_HEADER = "x-signature-ed25519"
QQ_OFFICIAL_WEBHOOK_TIMESTAMP_HEADER = "x-signature-timestamp"
QQ_OFFICIAL_WEBHOOK_APP_ID_HEADER = "x-bot-appid"
QQ_OFFICIAL_WEBHOOK_ACK_OPCODE = 12
QQ_OFFICIAL_WEBHOOK_VALIDATION_OPCODE = 13


class QQOfficialWebhookError(RuntimeError):
    """Base error for QQ Official webhook handling failures."""


class QQOfficialWebhookConfigurationError(QQOfficialWebhookError):
    """Webhook adapter was created without required credentials."""


class QQOfficialWebhookPayloadError(QQOfficialWebhookError):
    """Webhook body or payload shape was invalid."""


class QQOfficialWebhookSignatureError(QQOfficialWebhookError):
    """Webhook signature verification failed."""


@dataclass(frozen=True)
class QQOfficialWebhookValidationResponse:
    plain_token: str
    signature: str


@dataclass(frozen=True)
class QQOfficialWebhookParseResult:
    event: QQOfficialInboundEvent | None = None
    validation_response: QQOfficialWebhookValidationResponse | None = None


class QQOfficialWebhookAdapter:
    """Validate QQ Official webhook requests without touching storage."""

    def __init__(
        self,
        *,
        bot_secret: str,
        app_id: str | None = None,
    ) -> None:
        if not bot_secret.strip():
            raise QQOfficialWebhookConfigurationError(
                "QQ Official webhook Bot Secret is required"
            )
        self._app_id = app_id.strip() if app_id is not None else None
        self._private_key = Ed25519PrivateKey.from_private_bytes(
            _secret_seed(bot_secret)
        )

    def parse_request(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> QQOfficialWebhookParseResult:
        payload = _json_payload(body)
        opcode = _integer_value(payload.get("op"))
        if opcode == QQ_OFFICIAL_WEBHOOK_VALIDATION_OPCODE:
            return QQOfficialWebhookParseResult(
                validation_response=self._validation_response(payload)
            )

        self._verify_headers(headers)
        self._verify_signature(headers=headers, body=body)
        return QQOfficialWebhookParseResult(
            event=inbound_event_from_webhook_payload(payload)
        )

    @staticmethod
    def ack_payload() -> dict[str, int]:
        return {"op": QQ_OFFICIAL_WEBHOOK_ACK_OPCODE}

    def _validation_response(
        self,
        payload: Mapping[str, Any],
    ) -> QQOfficialWebhookValidationResponse:
        data = payload.get("d")
        if not isinstance(data, Mapping):
            raise QQOfficialWebhookPayloadError(
                "QQ Official webhook validation payload data must be an object"
            )
        plain_token = _required_string(data.get("plain_token"), "plain_token")
        event_ts = _required_string(data.get("event_ts"), "event_ts")
        signature_bytes = self._private_key.sign(
            event_ts.encode("utf-8") + plain_token.encode("utf-8")
        )
        return QQOfficialWebhookValidationResponse(
            plain_token=plain_token,
            signature=signature_bytes.hex(),
        )

    def _verify_headers(self, headers: Mapping[str, str]) -> None:
        if self._app_id is None:
            return
        header_app_id = _header_value(headers, QQ_OFFICIAL_WEBHOOK_APP_ID_HEADER)
        if header_app_id is not None and header_app_id != self._app_id:
            raise QQOfficialWebhookSignatureError(
                "QQ Official webhook AppID header did not match configured bot"
            )

    def _verify_signature(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        signature_hex = _required_header(
            headers, QQ_OFFICIAL_WEBHOOK_SIGNATURE_HEADER
        )
        timestamp = _required_header(headers, QQ_OFFICIAL_WEBHOOK_TIMESTAMP_HEADER)
        try:
            signature = bytes.fromhex(signature_hex)
        except ValueError as error:
            raise QQOfficialWebhookSignatureError(
                "QQ Official webhook signature was not valid hex"
            ) from error
        if len(signature) != 64:
            raise QQOfficialWebhookSignatureError(
                "QQ Official webhook signature had an invalid length"
            )
        message = timestamp.encode("utf-8") + body
        try:
            self._private_key.public_key().verify(signature, message)
        except InvalidSignature as error:
            raise QQOfficialWebhookSignatureError(
                "QQ Official webhook signature verification failed"
            ) from error


def _json_payload(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise QQOfficialWebhookPayloadError(
            "QQ Official webhook body was not valid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise QQOfficialWebhookPayloadError(
            "QQ Official webhook payload must be a JSON object"
        )
    return copy.deepcopy(payload)


def _secret_seed(secret: str) -> bytes:
    seed = secret.encode("utf-8")
    while len(seed) < 32:
        seed = seed * 2
    return seed[:32]


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _required_header(headers: Mapping[str, str], name: str) -> str:
    value = _header_value(headers, name)
    if value is None or not value.strip():
        raise QQOfficialWebhookSignatureError(
            f"QQ Official webhook header {name} is required"
        )
    return value


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise QQOfficialWebhookPayloadError(
            f"QQ Official webhook validation payload did not contain {field_name}"
        )
    return value


def _integer_value(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
