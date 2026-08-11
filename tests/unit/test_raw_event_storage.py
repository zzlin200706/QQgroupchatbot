from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.adapters.qq_official.gateway import QQGatewayDispatch
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.storage.database import Database
from app.storage.raw_event_repository import RawEventRepository


def dispatch(
    *,
    event_type: str = "GROUP_MESSAGE_CREATE",
    event_id: str = "event-1",
    sequence: int = 2,
    content: str = "hello",
) -> QQGatewayDispatch:
    return QQGatewayDispatch(
        sequence=sequence,
        event_type=event_type,
        data={
            "id": "message-1",
            "message_type": 0,
            "timestamp": "2026-08-10T17:10:22+08:00",
            "group_id": "group-1",
            "group_openid": "group-1",
            "author": {
                "id": "member-1",
                "member_openid": "member-1",
                "username": "测试成员",
            },
            "content": content,
            "future": {"kept": True},
        },
        event_id=event_id,
    )


async def create_storage(tmp_path: Path) -> tuple[Database, RawEventRepository]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'raw-events.db'}")
    await database.initialize()
    return database, RawEventRepository(database.session_factory)


@pytest.mark.asyncio
async def test_dispatch_round_trips_with_parser_envelope_and_safe_indexes(
    tmp_path: Path,
) -> None:
    database, repository = await create_storage(tmp_path)
    service = QQOfficialRawEventIngestionService(repository)
    event = dispatch()
    try:
        stored = await service.ingest(event)
        assert stored is not None

        loaded = await repository.get_by_id(stored.id)
        assert loaded is not None
        assert loaded.platform == "qq_official"
        assert loaded.message_type == "0"
        assert loaded.sub_type == "GROUP_MESSAGE_CREATE"
        assert loaded.user_id == "member-1"
        assert loaded.group_id == "group-1"
        assert loaded.message_id == "message-1"
        assert loaded.event_time is not None
        assert loaded.raw_payload == {
            "id": "event-1",
            "op": 0,
            "s": 2,
            "t": "GROUP_MESSAGE_CREATE",
            "d": event.data,
        }
        assert loaded.payload_hash == QQOfficialRawEventIngestionService.payload_hash(
            loaded.raw_payload
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_event_id_is_preserved_in_authoritative_raw_payload(tmp_path: Path) -> None:
    database, repository = await create_storage(tmp_path)
    service = QQOfficialRawEventIngestionService(repository)
    event = dispatch(event_id="qq-event-123")
    try:
        stored = await service.ingest(event)
        assert stored is not None
        loaded = await repository.get_by_id(stored.id)
        assert loaded is not None
        assert loaded.raw_payload["id"] == "qq-event-123"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_group_openid_index_is_preferred_over_legacy_group_id(tmp_path: Path) -> None:
    database, repository = await create_storage(tmp_path)
    service = QQOfficialRawEventIngestionService(repository)
    event = dispatch()
    event.data["group_id"] = "legacy-group-id"
    event.data["group_openid"] = "official-group-openid"
    try:
        stored = await service.ingest(event)
        assert stored is not None
        loaded = await repository.get_by_id(stored.id)
        assert loaded is not None
        assert loaded.group_id == "official-group-openid"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_member_openid_is_used_when_author_id_is_missing(tmp_path: Path) -> None:
    database, repository = await create_storage(tmp_path)
    service = QQOfficialRawEventIngestionService(repository)
    event = dispatch()
    del event.data["author"]["id"]
    event.data["author"]["member_openid"] = "member-openid-only"
    try:
        stored = await service.ingest(event)
        assert stored is not None
        loaded = await repository.get_by_id(stored.id)
        assert loaded is not None
        assert loaded.user_id == "member-openid-only"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_unknown_dispatch_fields_are_preserved_without_interpretation(
    tmp_path: Path,
) -> None:
    database, repository = await create_storage(tmp_path)
    service = QQOfficialRawEventIngestionService(repository)
    event = dispatch()
    try:
        stored = await service.ingest(event)
        assert stored is not None
        loaded = await repository.get_by_id(stored.id)
        assert loaded is not None
        assert loaded.raw_payload["d"]["future"] == {"kept": True}
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_missing_optional_indexes_are_stored_as_null(tmp_path: Path) -> None:
    database, repository = await create_storage(tmp_path)
    service = QQOfficialRawEventIngestionService(repository)
    event = QQGatewayDispatch(
        sequence=None,
        event_type="UNKNOWN_EVENT",
        data={"future": {"x": 1}},
    )
    try:
        stored = await service.ingest(event)
        assert stored is not None
        loaded = await repository.get_by_id(stored.id)
        assert loaded is not None
        assert loaded.message_type is None
        assert loaded.user_id is None
        assert loaded.group_id is None
        assert loaded.message_id is None
        assert loaded.event_time is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_storage_failure_returns_none_and_logs_safely(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, repository = await create_storage(tmp_path)

    class FailingOnceRepository:
        async def insert(self, event: object) -> object:
            raise RuntimeError("simulated SQLite failure")

    service = QQOfficialRawEventIngestionService(FailingOnceRepository())  # type: ignore[arg-type]
    caplog.set_level(logging.ERROR, logger="app.services.raw_event_ingestion")

    stored = await service.ingest(dispatch())

    assert stored is None
    assert "qq official raw event persistence failed" in caplog.text
    assert "hello" not in caplog.text


@pytest.mark.asyncio
async def test_database_creates_missing_sqlite_parent_directory(tmp_path: Path) -> None:
    database_path = tmp_path / "new-parent" / "raw-events.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    try:
        await database.initialize()
        assert database_path.is_file()
    finally:
        await database.dispose()
