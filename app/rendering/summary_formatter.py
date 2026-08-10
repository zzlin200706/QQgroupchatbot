"""Pure presentation of validated summaries for a QQ group message."""

from __future__ import annotations

from app.domain.summaries import SummaryResult


class SummaryMessageFormatter:
    """Render all validated summary fields without truncation or inference."""

    def format(self, result: SummaryResult) -> str:
        sections = ["【群聊总结】", "", result.summary]
        self._append_list(sections, "主要话题", result.topics)
        self._append_list(sections, "关键内容", result.key_points)
        self._append_list(sections, "决定", result.decisions)
        if result.action_items:
            sections.extend(("", "【待办】"))
            for item in result.action_items:
                sections.append(f"- {item.description}")
                sections.append(f"  负责人：{item.owner or '未知'}")
                sections.append(f"  截止：{item.deadline or '未知'}")
        self._append_list(sections, "未解决问题", result.open_questions)
        return "\n".join(sections)

    @staticmethod
    def _append_list(lines: list[str], title: str, items: tuple[str, ...]) -> None:
        if not items:
            return
        lines.extend(("", f"【{title}】"))
        lines.extend(f"- {item}" for item in items)
