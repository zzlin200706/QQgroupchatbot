"""Precedence-aware routing for commands and assistant interactions."""

from __future__ import annotations

from app.domain.messages import InternalMessage
from app.services.command_dispatch import QQOfficialCommandDispatcher
from app.services.group_assistant_handler import GroupAssistantHandler


class QQOfficialInteractionDispatcher:
    """Run explicit commands first, assistant triggers second, otherwise nothing."""

    def __init__(
        self,
        *,
        command_dispatcher: QQOfficialCommandDispatcher,
        assistant_handler: GroupAssistantHandler | None = None,
    ) -> None:
        self._command_dispatcher = command_dispatcher
        self._assistant_handler = assistant_handler

    def interaction_name(self, message: InternalMessage) -> str | None:
        command_name = self._command_dispatcher.command_name(message)
        if command_name is not None:
            return command_name
        if self._assistant_handler is None:
            return None
        trigger = self._assistant_handler.detect(message)
        return None if trigger is None else trigger.trigger_type.value

    async def handle(self, message: InternalMessage) -> object | None:
        command_name = self._command_dispatcher.command_name(message)
        if command_name is not None:
            return await self._command_dispatcher.handle(message)
        if self._assistant_handler is None:
            return None
        if self._assistant_handler.detect(message) is None:
            return None
        return await self._assistant_handler.handle(message)
