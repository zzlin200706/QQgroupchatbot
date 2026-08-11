"""Store complete QQ Official Gateway dispatches before semantic parsing."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.adapters.qq_official.gateway import QQGatewayDispatch
from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


logger = logging.getLogger(__name__)


class QQOfficialRawEventIngestionService:
    """Persist one QQ Official dispatch in the parser's stored envelope shape."""

    def __init__(self, repository: RawEventRepository) -> None:
        self._repository = repository

    async def ingest(self, dispatch: QQGatewayDispatch) -> RawEvent | None:
        """Persist one receipt, logging a safe summary if storage fails."""

        payload = {
            "gateway": {
                "op": 0,
                "s": dispatch.sequence,
                "t": dispatch.event_type,
            },
            "data": dispatch.data,
        }
        metadata = _dispatch_metadata(payload)
        payload_hash = self.payload_hash(payload)
        try:
            raw_event = RawEvent(
                platform="qq_official",
                received_at=datetime.now(timezone.utc),
                event_time=metadata["event_time"],
                post_type=None,
                message_type=metadata["message_type"],
                sub_type=metadata["event_type"],
                self_id=None,
                user_id=metadata["user_id"],
                group_id=metadata["group_id"],
                message_id=metadata["message_id"],
                raw_payload=payload,
                payload_hash=payload_hash,
            )
            return await self._repository.insert(raw_event)
        except Exception:
            logger.exception(
                "qq official raw event persistence failed event_type=%s message_id=%s payload_hash=%s",
                metadata["event_type"],
                metadata["message_id"],
                payload_hash,
            )
            return None

    @staticmethod
    def payload_hash(event: dict[str, Any]) -> str:
        """Return a deterministic SHA-256 fingerprint for diagnostics only."""

        serialized = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _dispatch_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    gateway = payload.get("gateway")
    data = payload.get("data")
    if not isinstance(gateway, dict) or not isinstance(data, dict):
        return {
            "event_time": None,
            "event_type": None,
            "message_type": None,
            "user_id": None,
            "group_id": None,
            "message_id": None,
        }

    author = data.get("author")
    user_identifier = None
    if isinstance(author, dict):
        user_identifier = _string_index(author.get("id")) or _string_index(
            author.get("member_openid")
        )
    return {
        "event_time": _event_time(data.get("timestamp")),
        "event_type": _string_index(gateway.get("t")),
        "message_type": _string_index(data.get("message_type")),
        "user_id": user_identifier,
        "group_id": _string_index(data.get("group_openid"))
        or _string_index(data.get("group_id")),
        "message_id": _string_index(data.get("id")),
    }


def _string_index(value: object) -> str | None:
    """Safely copy scalar IDs/types into a nullable text index."""

    if value is None or isinstance(value, (bool, dict, list)):
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _event_time(value: object) -> datetime | None:
    """Interpret one QQ Official RFC3339 timestamp as a UTC datetime."""

    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None
