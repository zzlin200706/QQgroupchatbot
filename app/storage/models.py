"""ORM models for persistence that precedes message parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Index, Integer, JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC datetimes consistently despite SQLite lacking tzinfo support."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime requires a timezone-aware datetime")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for the storage schema."""


class RawEvent(Base):
    """One receipt of a complete OneBot business-event payload.

    The extracted columns are optional indexes only. `raw_payload` remains the
    authoritative, lossless input for later normalization and parsing.
    """

    __tablename__ = "raw_events"
    __table_args__ = (
        Index("ix_raw_events_received_at", "received_at"),
        Index("ix_raw_events_group_id_received_at", "group_id", "received_at"),
        Index("ix_raw_events_payload_hash", "payload_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    event_time: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    post_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sub_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    self_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
