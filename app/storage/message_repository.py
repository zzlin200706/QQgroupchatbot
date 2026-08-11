"""Transactional persistence and retrieval of normalized message trees."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.messages.models import InternalMessage
from app.storage.message_codec import decode_message, encode_message
from app.storage.models import MessageNodeRecord, MessageRecord


class MessageRepository:
    """Persist one complete message tree per raw receipt/parser version."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def persist(
        self,
        message: InternalMessage,
        *,
        parser_name: str,
        parser_version: str,
    ) -> InternalMessage:
        """Atomically insert or return the existing idempotent representation."""

        record = encode_message(
            message,
            parser_name=parser_name,
            parser_version=parser_version,
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    existing = await self._find_version(
                        session,
                        raw_event_id=record.source_raw_event_id,
                        parser_name=parser_name,
                        parser_version=parser_version,
                    )
                    if existing is not None:
                        return await self._decode(session, existing)
                    session.add(record)
                    await session.flush()
                return await self._decode(session, record)
        except IntegrityError:
            # The unique constraint is the final authority if two workers race.
            existing = await self.get_by_raw_event_id(
                record.source_raw_event_id,
                parser_name=parser_name,
                parser_version=parser_version,
            )
            if existing is None:
                raise
            return existing

    async def get_by_id(self, message_id: int) -> InternalMessage | None:
        async with self._session_factory() as session:
            record = await session.get(MessageRecord, message_id)
            return None if record is None else await self._decode(session, record)

    async def get_by_raw_event_id(
        self,
        raw_event_id: int,
        *,
        parser_name: str | None = None,
        parser_version: str | None = None,
    ) -> InternalMessage | None:
        async with self._session_factory() as session:
            statement = select(MessageRecord).where(
                MessageRecord.source_raw_event_id == raw_event_id
            )
            if parser_name is not None:
                statement = statement.where(MessageRecord.parser_name == parser_name)
            if parser_version is not None:
                statement = statement.where(MessageRecord.parser_version == parser_version)
            record = await session.scalar(statement.order_by(MessageRecord.id.desc()).limit(1))
            return None if record is None else await self._decode(session, record)

    async def list_conversation(
        self,
        *,
        platform: str,
        group_id: str,
        start_time: datetime,
        end_time: datetime,
        limit: int,
        parser_name: str | None = None,
        parser_version: str | None = None,
    ) -> Sequence[InternalMessage]:
        """Decode a stable time window from normalized storage only.

        Selection happens before the conversation filters: for every raw
        receipt, the greatest normalized ``messages.id`` is authoritative in
        the requested parser scope. This prevents an older replay version from
        resurfacing merely because the latest representation changed a field.
        """

        candidates = select(
            MessageRecord.source_raw_event_id,
            func.max(MessageRecord.id).label("latest_id"),
        )
        if parser_name is not None:
            candidates = candidates.where(MessageRecord.parser_name == parser_name)
        if parser_version is not None:
            candidates = candidates.where(MessageRecord.parser_version == parser_version)
        latest_per_receipt = candidates.group_by(
            MessageRecord.source_raw_event_id
        ).subquery()

        statement = (
            select(MessageRecord)
            .join(latest_per_receipt, MessageRecord.id == latest_per_receipt.c.latest_id)
            .where(
                MessageRecord.platform == platform,
                MessageRecord.group_id == group_id,
                MessageRecord.timestamp.is_not(None),
                MessageRecord.timestamp >= start_time,
                MessageRecord.timestamp < end_time,
            )
            .order_by(MessageRecord.timestamp.asc(), MessageRecord.id.asc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            records = list((await session.scalars(statement)).all())
            if not records:
                return ()
            record_ids = [record.id for record in records]
            nodes = (
                await session.scalars(
                    select(MessageNodeRecord).where(
                        MessageNodeRecord.message_id.in_(record_ids)
                    )
                )
            ).all()
            nodes_by_message_id: dict[int, list[MessageNodeRecord]] = defaultdict(list)
            for node in nodes:
                nodes_by_message_id[node.message_id].append(node)
            return tuple(
                decode_message(record, nodes_by_message_id[record.id])
                for record in records
            )

    async def list_conversation_before(
        self,
        *,
        platform: str,
        group_id: str,
        start_time: datetime,
        trigger_time: datetime,
        trigger_raw_event_id: int | None,
        limit: int,
    ) -> Sequence[InternalMessage]:
        """Return the newest messages strictly preceding one trigger receipt."""

        latest_per_receipt = (
            select(
                MessageRecord.source_raw_event_id,
                func.max(MessageRecord.id).label("latest_id"),
            )
            .group_by(MessageRecord.source_raw_event_id)
            .subquery()
        )
        before_trigger = MessageRecord.timestamp < trigger_time
        if trigger_raw_event_id is not None:
            before_trigger = or_(
                before_trigger,
                and_(
                    MessageRecord.timestamp == trigger_time,
                    MessageRecord.source_raw_event_id < trigger_raw_event_id,
                ),
            )
        statement = (
            select(MessageRecord)
            .join(latest_per_receipt, MessageRecord.id == latest_per_receipt.c.latest_id)
            .where(
                MessageRecord.platform == platform,
                MessageRecord.group_id == group_id,
                MessageRecord.timestamp.is_not(None),
                MessageRecord.timestamp >= start_time,
                before_trigger,
            )
            .order_by(
                MessageRecord.timestamp.desc(),
                MessageRecord.source_raw_event_id.desc(),
                MessageRecord.id.desc(),
            )
            .limit(limit)
        )
        async with self._session_factory() as session:
            records = list((await session.scalars(statement)).all())
            records.reverse()
            if not records:
                return ()
            record_ids = [record.id for record in records]
            nodes = (
                await session.scalars(
                    select(MessageNodeRecord).where(
                        MessageNodeRecord.message_id.in_(record_ids)
                    )
                )
            ).all()
            nodes_by_message_id: dict[int, list[MessageNodeRecord]] = defaultdict(list)
            for node in nodes:
                nodes_by_message_id[node.message_id].append(node)
            return tuple(
                decode_message(record, nodes_by_message_id[record.id])
                for record in records
            )

    async def _find_version(
        self,
        session: AsyncSession,
        *,
        raw_event_id: int,
        parser_name: str,
        parser_version: str,
    ) -> MessageRecord | None:
        return await session.scalar(
            select(MessageRecord).where(
                MessageRecord.source_raw_event_id == raw_event_id,
                MessageRecord.parser_name == parser_name,
                MessageRecord.parser_version == parser_version,
            )
        )

    async def _decode(
        self,
        session: AsyncSession,
        record: MessageRecord,
    ) -> InternalMessage:
        nodes = (
            await session.scalars(
                select(MessageNodeRecord).where(
                    MessageNodeRecord.message_id == record.id
                )
            )
        ).all()
        return decode_message(record, nodes)
