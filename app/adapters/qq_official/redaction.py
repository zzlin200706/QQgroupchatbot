"""Safe serialization helpers for local QQ Official Gateway samples."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.adapters.qq_official.gateway import QQGatewayDispatch


_AUTH_TOKEN_PATTERN = re.compile(r"(?i)(auth_token=)([^&\s]+)")
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "app_secret",
        "auth_token",
        "authorization",
        "client_secret",
        "cookie",
        "password",
        "private_key",
        "refresh_token",
        "token",
    }
)


def redact_gateway_dispatch(
    dispatch: QQGatewayDispatch,
    *,
    captured_at: str,
) -> dict[str, object]:
    """Return a loss-preserving, recursively redacted Gateway Dispatch envelope."""

    return {
        "captured_at": captured_at,
        "gateway": {"op": 0, "s": dispatch.sequence, "t": dispatch.event_type},
        "data": redact_value(dispatch.data),
    }


def redact_value(value: object, *, parent_key: str | None = None) -> object:
    """Deep-copy a JSON-like value while removing credential-bearing values."""

    if isinstance(value, Mapping):
        redacted: dict[object, object] = {}
        for key, child in value.items():
            key_name = key if isinstance(key, str) else None
            if key_name is not None and key_name.lower() in _SENSITIVE_KEYS:
                redacted[key] = "<REDACTED>"
            else:
                redacted[key] = redact_value(child, parent_key=key_name)
        return redacted
    if isinstance(value, list):
        return [redact_value(child, parent_key=parent_key) for child in value]
    if isinstance(value, tuple):
        return [redact_value(child, parent_key=parent_key) for child in value]
    if isinstance(value, str):
        if parent_key == "ext":
            return _AUTH_TOKEN_PATTERN.sub(r"\1<REDACTED>", value)
        if parent_key == "url":
            return redact_url(value)
    return copy.deepcopy(value)


def redact_url(value: str) -> str:
    """Retain a URL host/path while redacting every query value."""

    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc or not parsed.query:
        return value
    query = urlencode([(key, "<REDACTED>") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def dispatch_summary(dispatch: QQGatewayDispatch) -> dict[str, object]:
    """Return safe structural metadata without exposing message content or IDs."""

    data = dispatch.data if isinstance(dispatch.data, Mapping) else {}
    msg_elements = data.get("msg_elements")
    elements = msg_elements if isinstance(msg_elements, list) else []
    structure = _element_structure(elements)
    author = data.get("author")
    return {
        "event_type": dispatch.event_type,
        "message_type": _safe_scalar(data.get("message_type")),
        "author_present": isinstance(author, Mapping),
        "msg_elements": len(elements),
        "max_nested_depth": structure["max_depth"],
        "nested_author_present": structure["author_present"],
        "nested_author_missing": structure["author_missing"],
        "mentions": _list_length(data.get("mentions")),
        "attachments": _list_length(data.get("attachments")),
    }


def duplicate_key(dispatch: QQGatewayDispatch) -> tuple[int | None, str | int | None]:
    """Return informational sequence/message identifiers without deduplicating."""

    data = dispatch.data if isinstance(dispatch.data, Mapping) else {}
    message_id = data.get("id")
    return (
        dispatch.sequence,
        message_id if isinstance(message_id, (str, int)) and not isinstance(message_id, bool) else None,
    )


def is_duplicate_candidate(
    manifest: list[Mapping[str, object]],
    dispatch: QQGatewayDispatch,
) -> bool:
    """Detect an informational duplicate only from a stable event message ID.

    Gateway sequence values are scoped to one WebSocket session, so they must
    not be compared across independently started capture runs.
    """

    _, message_id = duplicate_key(dispatch)
    if message_id is None:
        return False
    return any(
        item.get("event_type") == dispatch.event_type and item.get("message_id") == message_id
        for item in manifest
    )


def _element_structure(elements: list[object]) -> dict[str, int]:
    max_depth = 0
    author_present = 0
    author_missing = 0

    def visit(value: object, depth: int, *, element: bool = False) -> None:
        nonlocal max_depth, author_present, author_missing
        if isinstance(value, Mapping):
            max_depth = max(max_depth, depth)
            if element:
                if isinstance(value.get("author"), Mapping):
                    author_present += 1
                else:
                    author_missing += 1
            for key in ("msg_elements", "children"):
                children = value.get(key)
                if isinstance(children, list):
                    for child in children:
                        visit(child, depth + 1, element=key == "msg_elements")
        elif isinstance(value, list):
            for child in value:
                visit(child, depth, element=True)

    visit(elements, 0)
    return {
        "max_depth": max_depth,
        "author_present": author_present,
        "author_missing": author_missing,
    }


def _safe_scalar(value: object) -> str | int | float | bool | None:
    return value if isinstance(value, (str, int, float, bool)) else None


def _list_length(value: object) -> int:
    return len(value) if isinstance(value, list) else 0
