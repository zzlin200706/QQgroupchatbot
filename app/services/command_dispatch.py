"""Minimal QQ Official command routing for exact group commands."""

from __future__ import annotations

from app.domain.messages import InternalMessage
from app.services.ping_command import PingCommandHandler, is_ping_command
from app.services.summary_command import SummaryCommandHandler, is_summary_command


class QQOfficialCommandDispatcher:
    """Route exact commands to the corresponding command handlers."""

    def __init__(
        self,
        *,
        ping_handler: PingCommandHandler,
        summary_handler: SummaryCommandHandler | None = None,
    ) -> None:
        self._ping_handler = ping_handler
        self._summary_handler = summary_handler

    def command_name(self, message: InternalMessage) -> str | None:
        if is_ping_command(message):
            return "ping"
        if self._summary_handler is not None and is_summary_command(message):
            return "summary"
        return None

    async def handle(self, message: InternalMessage) -> object | None:
        command_name = self.command_name(message)
        if command_name == "ping":
            return await self._ping_handler.handle(message)
        if command_name == "summary":
            assert self._summary_handler is not None
            return await self._summary_handler.handle(message)
        return None
