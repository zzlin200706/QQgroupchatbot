"""ORM models for raw receipts, normalized message trees, and summaries."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
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


class MessageRecord(Base):
    """One versioned normalized representation of a raw event receipt."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "source_raw_event_id",
            "parser_name",
            "parser_version",
            name="uq_messages_raw_parser_version",
        ),
        Index("ix_messages_source_raw_event_id", "source_raw_event_id"),
        Index("ix_messages_group_id_timestamp", "group_id", "timestamp"),
        Index("ix_messages_platform_message_id", "platform", "platform_message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_raw_event_id: Mapped[int] = mapped_column(
        ForeignKey("raw_events.id", ondelete="RESTRICT"),
        nullable=False,
    )

    parser_name: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    message_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sub_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    actor_platform: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    actor_card: Mapped[str | None] = mapped_column(String(256), nullable=True)
    actor_source: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_availability: Mapped[str] = mapped_column(String(32), nullable=False)

    author_platform: Mapped[str] = mapped_column(String(32), nullable=False)
    author_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    author_display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    author_card: Mapped[str | None] = mapped_column(String(256), nullable=True)
    author_source: Mapped[str] = mapped_column(String(32), nullable=False)
    author_availability: Mapped[str] = mapped_column(String(32), nullable=False)

    provenance_source: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance_raw_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provenance_parent_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    provenance_forward_depth: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    nodes: Mapped[list["MessageNodeRecord"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        foreign_keys="MessageNodeRecord.message_id",
    )


class MessageNodeRecord(Base):
    """One typed object in a normalized message's recursive domain tree."""

    __tablename__ = "message_nodes"
    __table_args__ = (
        Index(
            "ix_message_nodes_tree_order",
            "message_id",
            "parent_node_id",
            "relation",
            "position",
        ),
        Index("ix_message_nodes_message_kind", "message_id", "node_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_node_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_nodes.id", ondelete="CASCADE"),
        nullable=True,
    )
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    node_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)

    author_platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    author_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    author_display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    author_card: Mapped[str | None] = mapped_column(String(256), nullable=True)
    author_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    author_availability: Mapped[str | None] = mapped_column(String(32), nullable=True)

    provenance_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provenance_raw_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provenance_parent_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    provenance_forward_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    message: Mapped[MessageRecord] = relationship(
        back_populates="nodes",
        foreign_keys=[message_id],
    )
    parent: Mapped["MessageNodeRecord | None"] = relationship(
        back_populates="children",
        foreign_keys=[parent_node_id],
        remote_side=[id],
    )
    children: Mapped[list["MessageNodeRecord"]] = relationship(
        back_populates="parent",
        foreign_keys=[parent_node_id],
    )


class SummaryRecord(Base):
    """One immutable historical run produced from a validated SummaryResult."""

    __tablename__ = "summaries"
    __table_args__ = (
        Index(
            "ix_summaries_platform_group_window",
            "platform",
            "group_id",
            "start_time",
            "end_time",
        ),
        Index(
            "ix_summaries_platform_group_created_at",
            "platform",
            "group_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    group_id: Mapped[str] = mapped_column(String(128), nullable=False)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    end_time: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    topics: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    key_points: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    decisions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    action_items: Mapped[list[dict[str, str | None]]] = mapped_column(
        JSON,
        nullable=False,
    )
    open_questions: Mapped[list[str]] = mapped_column(JSON, nullable=False)

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    finish_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
