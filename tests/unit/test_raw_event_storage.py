import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import websockets

from app.adapters.onebot.client import OneBotClient
from app.services.raw_event_ingestion import RawEventIngestionService
from app.storage.database import Database
from app.storage.raw_event_repository import RawEventRepository


FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "fixtures"
    / "onebot"
    / "real_group_text_sanitized.json"
)


def fixture_event() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


async def create_storage(tmp_path: Path) -> tuple[Database, RawEventRepository]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'raw-events.db'}")
    await database.initialize()
    return database, RawEventRepository(database.session_factory)


async def wait_for_condition(predicate: Callable[[], Awaitable[bool]]) -> None:
    async def condition_is_true() -> None:
        while not await predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(condition_is_true(), timeout=1)


@pytest.mark.asyncio
async def test_raw_event_fixture_round_trips_with_index_fields(tmp_path: Path) -> None:
    database, repository = await create_storage(tmp_path)
    event = fixture_event()
    service = RawEventIngestionService(repository)
    try:
        stored = await service.ingest(event)
        assert stored is not None

        loaded = await repository.get_by_id(stored.id)
        assert loaded is not None
        assert loaded.raw_payload == event
        assert loaded.platform == "onebot11"
        assert loaded.post_type == event["post_type"]
        assert loaded.message_type == event["message_type"]
        assert loaded.sub_type == event["sub_type"]
        assert loaded.self_id == str(event["self_id"])
        assert loaded.user_id == str(event["user_id"])
        assert loaded.group_id == str(event["group_id"])
        assert loaded.message_id == str(event["message_id"])
        assert loaded.event_time is not None
        assert int(loaded.event_time.timestamp()) == event["time"]
        assert loaded.payload_hash == RawEventIngestionService.payload_hash(event)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_database_creates_missing_sqlite_parent_directory(tmp_path: Path) -> None:
    database_path = tmp_path / "new-parent" / "raw-events.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    try:
        await database.initialize()
        assert database_path.is_file()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_unknown_top_level_field_is_preserved(tmp_path: Path) -> None:
    database, repository = await create_storage(tmp_path)
    event: dict[str, Any] = {
        "post_type": "message",
        "unknown_future_field": {"foo": [1, 2, {"bar": True}]},
    }
    try:
        stored = await RawEventIngestionService(repository).ingest(event)
        assert stored is not None
        loaded = await repository.get_by_id(stored.id)
        assert loaded is not None
        assert loaded.raw_payload["unknown_future_field"] == event["unknown_future_field"]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_nested_json_tree_round_trips_without_forward_interpretation(tmp_path: Path) -> None:
    database, repository = await create_storage(tmp_path)
    event: dict[str, Any] = {
        "post_type": "message",
        "message": [
            {
                "type": "forward",
                "data": {
                    "nodes": [
                        {
                            "content": [
                                {
                                    "type": "forward",
                                    "data": {"nodes": [{"content": [{"type": "text"}]}]},
                                }
                            ]
                        }
                    ]
                },
            }
        ],
    }
    try:
        stored = await RawEventIngestionService(repository).ingest(event)
        assert stored is not None
        loaded = await repository.get_by_id(stored.id)
        assert loaded is not None
        assert loaded.raw_payload == event
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_missing_optional_indexes_are_stored_as_null(tmp_path: Path) -> None:
    database, repository = await create_storage(tmp_path)
    event: dict[str, Any] = {"post_type": "notice", "foo": "bar"}
    try:
        stored = await RawEventIngestionService(repository).ingest(event)
        assert stored is not None
        loaded = await repository.get_by_id(stored.id)
        assert loaded is not None
        assert loaded.post_type == "notice"
        assert loaded.message_type is None
        assert loaded.sub_type is None
        assert loaded.self_id is None
        assert loaded.user_id is None
        assert loaded.group_id is None
        assert loaded.message_id is None
        assert loaded.event_time is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_identical_payloads_are_kept_as_distinct_receipts(tmp_path: Path) -> None:
    database, repository = await create_storage(tmp_path)
    event = fixture_event()
    service = RawEventIngestionService(repository)
    try:
        first = await service.ingest(event)
        second = await service.ingest(event)
        assert first is not None
        assert second is not None
        assert first.id != second.id
        assert first.payload_hash == second.payload_hash
        assert len(await repository.list_recent()) == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_adapter_business_event_flows_into_raw_storage_and_skips_meta_events(
    tmp_path: Path,
) -> None:
    database, repository = await create_storage(tmp_path)
    service = RawEventIngestionService(repository)
    event = fixture_event()
    business_event_seen = asyncio.Event()

    async def event_handler(received_event: dict[str, object]) -> None:
        await service.ingest(received_event)
        business_event_seen.set()

    async def handler(connection: websockets.ServerConnection) -> None:
        await connection.send(
            json.dumps(
                {"post_type": "meta_event", "meta_event_type": "heartbeat", "interval": 30000}
            )
        )
        await connection.send(json.dumps(event))
        await connection.wait_closed()

    try:
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = OneBotClient(
                url=f"ws://127.0.0.1:{port}",
                event_handler=event_handler,
                reconnect_initial_delay=0.01,
                reconnect_max_delay=0.02,
            )
            await client.start()
            await asyncio.wait_for(business_event_seen.wait(), timeout=1)
            await wait_for_condition(lambda: _has_exactly_one_receipt(repository))
            await client.stop()

        records = await repository.list_recent()
        assert len(records) == 1
        assert records[0].raw_payload == event
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_storage_failure_does_not_stop_subsequent_adapter_events(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database, repository = await create_storage(tmp_path)
    first_event = {"post_type": "message", "message_id": 1}
    second_event = {"post_type": "message", "message_id": 2}

    class FailingOnceRepository:
        def __init__(self, delegate: RawEventRepository) -> None:
            self._delegate = delegate
            self._failed = False

        async def insert(self, event: Any) -> Any:
            if not self._failed:
                self._failed = True
                raise RuntimeError("simulated SQLite failure")
            return await self._delegate.insert(event)

    service = RawEventIngestionService(FailingOnceRepository(repository))  # type: ignore[arg-type]
    second_event_seen = asyncio.Event()

    async def event_handler(received_event: dict[str, object]) -> None:
        await service.ingest(received_event)
        if received_event.get("message_id") == 2:
            second_event_seen.set()

    async def handler(connection: websockets.ServerConnection) -> None:
        await connection.send(json.dumps(first_event))
        await connection.send(json.dumps(second_event))
        await connection.wait_closed()

    caplog.set_level(logging.ERROR, logger="app.services.raw_event_ingestion")
    try:
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = OneBotClient(
                url=f"ws://127.0.0.1:{port}",
                event_handler=event_handler,
                reconnect_initial_delay=0.01,
                reconnect_max_delay=0.02,
            )
            await client.start()
            await asyncio.wait_for(second_event_seen.wait(), timeout=1)
            await wait_for_condition(lambda: _has_exactly_one_receipt(repository))
            assert client.is_connected
            await client.stop()

        records = await repository.list_recent()
        assert len(records) == 1
        assert records[0].message_id == "2"
        assert "raw event persistence failed" in caplog.text
    finally:
        await database.dispose()


async def _has_exactly_one_receipt(repository: RawEventRepository) -> bool:
    return len(await repository.list_recent()) == 1
