from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.domain.summaries import SummaryActionItem, SummaryResult
from app.storage.database import Database
from app.storage.models import SummaryRecord
from app.storage.summary_repository import SummaryRepository


UTC = timezone.utc
START = datetime(2026, 8, 10, 8, tzinfo=timezone(timedelta(hours=8)))
END = datetime(2026, 8, 11, 8, tzinfo=timezone(timedelta(hours=8)))
CREATED = datetime(2026, 8, 11, 1, 2, 3, tzinfo=timezone(timedelta(hours=8)))


async def repository(
    tmp_path: Path,
    *,
    clock=lambda: CREATED,
) -> tuple[Database, SummaryRepository]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'summaries.db'}")
    await database.initialize()
    return database, SummaryRepository(database.session_factory, clock=clock)


def result(
    *,
    platform: str = "qq_official",
    group_id: str = "group-a",
    summary: str = "总体摘要",
    prompt_tokens: int | None = 100,
    completion_tokens: int | None = 50,
    total_tokens: int | None = 150,
) -> SummaryResult:
    return SummaryResult(
        platform=platform,
        group_id=group_id,
        start_time=START,
        end_time=END,
        message_count=3,
        summary=summary,
        topics=("主题一", "主题二"),
        key_points=("重点一", "重点二"),
        decisions=("决定一",),
        action_items=(
            SummaryActionItem(
                description="准备材料",
                owner="Alice",
                deadline="周二前",
            ),
            SummaryActionItem(
                description="等待确认",
                owner=None,
                deadline=None,
            ),
        ),
        open_questions=("问题一", "问题二"),
        provider="deepseek",
        model="deepseek-v4-flash",
        finish_reason="stop",
        input_chars=1234,
        prompt_version="1",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


@pytest.mark.asyncio
async def test_normal_persistence_get_by_id_and_structured_round_trip(tmp_path: Path) -> None:
    database, summaries = await repository(tmp_path)
    original = result()
    try:
        stored = await summaries.persist(original)
        loaded = await summaries.get_by_id(stored.id)

        assert stored.id > 0
        assert loaded == stored
        assert stored.result == original
        assert stored.result.topics == ("主题一", "主题二")
        assert stored.result.key_points == ("重点一", "重点二")
        assert stored.result.decisions == ("决定一",)
        assert stored.result.open_questions == ("问题一", "问题二")
        assert stored.result.action_items == original.action_items
        assert stored.result.action_items[1].owner is None
        assert stored.result.action_items[1].deadline is None
        assert stored.created_at == CREATED
        assert stored.created_at.tzinfo is UTC
        assert stored.result.start_time.tzinfo is UTC
        assert stored.result.end_time.tzinfo is UTC
        assert stored.result.start_time.hour == 0
        assert stored.result.end_time.hour == 0
        assert await summaries.get_by_id(stored.id + 1000) is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt_tokens", "completion_tokens", "total_tokens"),
    [
        (100, 50, 150),
        (None, None, None),
        (100, None, None),
    ],
)
async def test_nullable_usage_is_never_replaced_with_zero(
    tmp_path: Path,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
) -> None:
    database, summaries = await repository(tmp_path)
    original = result(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    try:
        stored = await summaries.persist(original)

        assert stored.result.prompt_tokens == prompt_tokens
        assert stored.result.completion_tokens == completion_tokens
        assert stored.result.total_tokens == total_tokens
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_explicit_empty_window_result_is_valid_history(tmp_path: Path) -> None:
    database, summaries = await repository(tmp_path)
    empty = replace(
        result(),
        message_count=0,
        summary="该时间范围内没有可总结的消息。",
        topics=(),
        key_points=(),
        decisions=(),
        action_items=(),
        open_questions=(),
        provider="none",
        model="none",
        finish_reason=None,
        input_chars=0,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
    )
    try:
        stored = await summaries.persist(empty)

        assert stored.result == empty
        assert stored.result.message_count == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_same_window_creates_distinct_history_rows_in_stable_newest_order(
    tmp_path: Path,
) -> None:
    timestamps = iter(
        (
            datetime(2026, 8, 11, 1, tzinfo=UTC),
            datetime(2026, 8, 11, 2, tzinfo=UTC),
            datetime(2026, 8, 11, 2, tzinfo=UTC),
        )
    )
    database, summaries = await repository(tmp_path, clock=lambda: next(timestamps))
    try:
        first = await summaries.persist(result(summary="run-one"))
        second = await summaries.persist(result(summary="run-two"))
        third = await summaries.persist(result(summary="run-three"))
        history = await summaries.list_for_group(
            platform="qq_official",
            group_id="group-a",
        )

        assert len({first.id, second.id, third.id}) == 3
        assert [item.id for item in history] == [third.id, second.id, first.id]
        assert [item.result.summary for item in history] == [
            "run-three",
            "run-two",
            "run-one",
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_group_platform_isolation_and_limit(tmp_path: Path) -> None:
    database, summaries = await repository(tmp_path)
    try:
        official_a_1 = await summaries.persist(result(summary="official-a-one"))
        official_a_2 = await summaries.persist(result(summary="official-a-two"))
        await summaries.persist(result(group_id="group-b", summary="official-b"))
        await summaries.persist(
            result(platform="other_platform", group_id="group-a", summary="other-a")
        )

        official_a = await summaries.list_for_group(
            platform="qq_official",
            group_id="group-a",
        )
        other_platform = await summaries.list_for_group(
            platform="other_platform",
            group_id="group-a",
        )
        limited = await summaries.list_for_group(
            platform="qq_official",
            group_id="group-a",
            limit=1,
        )

        assert [item.id for item in official_a] == [official_a_2.id, official_a_1.id]
        assert [item.result.summary for item in other_platform] == ["other-a"]
        assert [item.id for item in limited] == [official_a_2.id]
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"platform": "", "group_id": "group-a", "limit": 1},
        {"platform": "qq_official", "group_id": "", "limit": 1},
        {"platform": "qq_official", "group_id": "group-a", "limit": 0},
        {"platform": "qq_official", "group_id": "group-a", "limit": -1},
    ],
)
async def test_history_query_rejects_invalid_arguments(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    database, summaries = await repository(tmp_path)
    try:
        with pytest.raises(ValueError):
            await summaries.list_for_group(**kwargs)  # type: ignore[arg-type]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_failed_insert_rolls_back_without_a_partial_summary(tmp_path: Path) -> None:
    database, summaries = await repository(tmp_path)
    invalid_action = SummaryActionItem(
        description={"not-json-serializable"},  # type: ignore[arg-type]
        owner=None,
        deadline=None,
    )
    invalid = replace(result(), action_items=(invalid_action,))
    try:
        with pytest.raises(Exception):
            await summaries.persist(invalid)

        async with database.session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(SummaryRecord))
        assert count == 0
    finally:
        await database.dispose()


def test_summary_table_contains_only_validated_result_and_storage_metadata() -> None:
    columns = set(SummaryRecord.__table__.columns.keys())
    index_columns = {
        tuple(column.name for column in index.columns)
        for index in SummaryRecord.__table__.indexes
    }

    assert columns == {
        "id",
        "platform",
        "group_id",
        "start_time",
        "end_time",
        "message_count",
        "created_at",
        "summary",
        "topics",
        "key_points",
        "decisions",
        "action_items",
        "open_questions",
        "provider",
        "model",
        "finish_reason",
        "input_chars",
        "prompt_version",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }
    forbidden = {
        "raw_payload",
        "raw_data",
        "rendered_text",
        "conversation",
        "system_prompt",
        "user_prompt",
        "response_body",
        "authorization",
        "api_key",
        "attachment_url",
        "attachment_path",
        "user_id",
        "author_id",
        "sender_id",
        "refidx",
    }
    assert columns.isdisjoint(forbidden)
    assert ("platform", "group_id", "start_time", "end_time") in index_columns
    assert ("platform", "group_id", "created_at") in index_columns
    assert not any(index.unique for index in SummaryRecord.__table__.indexes)
