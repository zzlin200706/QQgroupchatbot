"""Rebuild forward-containing normalized trees without replaying interactions."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.messages import ForwardSegment, ReplySegment
from app.domain.messages.segments import MessageSegment
from app.parsers import QQOfficialMessageParser
from app.services.normalized_message_ingestion import (
    QQ_OFFICIAL_PARSER_NAME,
    QQ_OFFICIAL_PARSER_VERSION,
)
from app.storage.message_repository import MessageRepository
from app.storage.models import MessageRecord, RawEvent


@dataclass
class ForwardReparseResult:
    scanned: int = 0
    candidates: int = 0
    persisted: int = 0
    failed: int = 0


def _contains_forward(segments: Sequence[MessageSegment]) -> bool:
    return any(
        isinstance(segment, ForwardSegment)
        or (
            isinstance(segment, ReplySegment)
            and segment.resolved_message is not None
            and _contains_forward(segment.resolved_message.segments)
        )
        for segment in segments
    )


async def reparse_forwards(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    parser: QQOfficialMessageParser,
    apply: bool = False,
    raw_event_id: int | None = None,
    batch_size: int = 100,
) -> ForwardReparseResult:
    """Append the current parser version, preserving raw and older versions.

    Only local parser/repository operations are used. No event processor,
    outbox, command handler, network client or model is constructed.
    The initial high-water mark bounds a run even when new events arrive.
    """
    if batch_size < 1 or (raw_event_id is not None and raw_event_id < 1):
        raise ValueError("batch_size and raw_event_id must be positive")
    result = ForwardReparseResult()
    repository = MessageRepository(session_factory)
    async with session_factory() as session:
        high_water = await session.scalar(select(func.max(RawEvent.id))) or 0
    cursor = 0
    while cursor < high_water:
        statement = select(RawEvent).where(
            RawEvent.platform == "qq_official",
            RawEvent.id > cursor,
            RawEvent.id <= high_water,
            ~select(MessageRecord.id).where(
                MessageRecord.source_raw_event_id == RawEvent.id,
                MessageRecord.parser_name == QQ_OFFICIAL_PARSER_NAME,
                MessageRecord.parser_version == QQ_OFFICIAL_PARSER_VERSION,
            ).exists(),
        )
        if raw_event_id is not None:
            statement = statement.where(RawEvent.id == raw_event_id)
        async with session_factory() as session:
            rows = list((await session.scalars(
                statement.order_by(RawEvent.id).limit(batch_size)
            )).all())
        if not rows:
            break
        cursor = rows[-1].id
        for raw in rows:
            result.scanned += 1
            try:
                message = parser.parse(raw.raw_payload, raw_event_id=raw.id)
                if message is None or not _contains_forward(message.segments):
                    continue
                result.candidates += 1
                if apply:
                    await repository.persist(
                        message, parser_name=QQ_OFFICIAL_PARSER_NAME,
                        parser_version=QQ_OFFICIAL_PARSER_VERSION,
                    )
                    result.persisted += 1
            except Exception:
                # Do not print the payload or modify the original receipt.
                # Count failures explicitly so the CLI cannot report success.
                result.failed += 1
    return result
