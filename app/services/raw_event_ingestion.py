"""Store complete OneBot business events before any semantic parsing."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from app.storage.models import RawEvent
from app.storage.raw_event_repository import RawEventRepository


logger = logging.getLogger(__name__)


class RawEventIngestionService:
    """Extract optional indexes and persist every received business event."""

    def __init__(self, repository: RawEventRepository) -> None:
        self._repository = repository

    async def ingest(self, event: dict[str, Any]) -> RawEvent | None:
        """Persist one receipt, logging a safe summary if storage fails.

        Returning ``None`` for a failed insert deliberately prevents a transient
        SQLite error from escaping the OneBot event callback and stopping receive
        processing. The full payload is never emitted to logs.
        """

        payload_hash = self.payload_hash(event)
        try:
            raw_event = RawEvent(
                platform="onebot11",
                received_at=datetime.now(timezone.utc),
                event_time=_event_time(event.get("time")),
                post_type=_string_index(event.get("post_type")),
                message_type=_string_index(event.get("message_type")),
                sub_type=_string_index(event.get("sub_type")),
                self_id=_string_index(event.get("self_id")),
                user_id=_string_index(event.get("user_id")),
                group_id=_string_index(event.get("group_id")),
                message_id=_string_index(event.get("message_id")),
                raw_payload=event,
                payload_hash=payload_hash,
            )
            return await self._repository.insert(raw_event)
        except Exception:
            logger.exception(
                "raw event persistence failed post_type=%s message_type=%s payload_hash=%s",
                _string_index(event.get("post_type")),
                _string_index(event.get("message_type")),
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


def _string_index(value: object) -> str | None:
    """Safely copy scalar top-level IDs/types into a nullable text index."""

    if value is None or isinstance(value, (bool, dict, list)):
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _event_time(value: object) -> datetime | None:
    """Interpret a finite numeric top-level `time` value as a UTC epoch.

    This affects only an optional query index; the original payload value is
    retained unchanged in `raw_payload` regardless of whether it is usable.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
