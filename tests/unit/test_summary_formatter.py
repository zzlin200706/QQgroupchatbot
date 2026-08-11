from __future__ import annotations

from datetime import datetime, timezone

from app.domain.summaries import SummaryActionItem, SummaryResult
from app.rendering import SummaryMessageFormatter


def result(**overrides: object) -> SummaryResult:
    values: dict[str, object] = {
        "platform": "qq_official",
        "group_id": "synthetic-group",
        "start_time": datetime(2026, 8, 10, tzinfo=timezone.utc),
        "end_time": datetime(2026, 8, 11, tzinfo=timezone.utc),
        "message_count": 3,
        "summary": "群内确认了会议安排。",
        "topics": ("会议",),
        "key_points": ("九点开始",),
        "decisions": ("按时开会",),
        "action_items": (
            SummaryActionItem("准备材料", "Alice", "周二前"),
            SummaryActionItem("确认场地", None, None),
        ),
        "open_questions": ("是否需要投影？",),
        "provider": "fake",
        "model": "fake-model",
        "finish_reason": "stop",
        "input_chars": 100,
        "prompt_version": "1",
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    values.update(overrides)
    return SummaryResult(**values)  # type: ignore[arg-type]


def test_formats_all_non_empty_sections_and_unknown_action_metadata() -> None:
    text = SummaryMessageFormatter().format(result())

    assert text == """【群聊总结】

群内确认了会议安排。

【主要话题】
- 会议

【关键内容】
- 九点开始

【决定】
- 按时开会

【待办】
- 准备材料
  负责人：Alice
  截止：周二前
- 确认场地
  负责人：未知
  截止：未知

【未解决问题】
- 是否需要投影？"""


def test_omits_empty_sections_without_truncating_summary() -> None:
    long_summary = "完整内容" * 1000
    text = SummaryMessageFormatter().format(
        result(
            summary=long_summary,
            topics=(),
            key_points=(),
            decisions=(),
            action_items=(),
            open_questions=(),
        )
    )

    assert text == f"【群聊总结】\n\n{long_summary}"
    assert "【主要话题】" not in text
    assert "【待办】" not in text
