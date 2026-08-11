"""QQ Official orchestration for the exact manual ``#ping`` command."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Protocol

from app.domain.messages import InternalMessage, ProvenanceSource, TextSegment


logger = logging.getLogger(__name__)


class PingCommandStatus(str, Enum):
    NOT_COMMAND = "not_command"
    INVALID_CONTEXT = "invalid_context"
    SEND_FAILED = "send_failed"
    SUCCEEDED = "succeeded"


class _GroupMessageSender(Protocol):
    async def send_group_message(
        self,
        group_id: str,
        message: str,
        *,
        msg_id: str | None = None,
    ) -> object: ...


def is_ping_command(message: InternalMessage) -> bool:
    """Match only exact text from the direct message's top-level segments."""

    if message.provenance.source_type is not ProvenanceSource.DIRECT_EVENT:
        return False
    if not message.segments or any(
        not isinstance(segment, TextSegment) for segment in message.segments
    ):
        return False
    return "".join(segment.text or "" for segment in message.segments).strip() == "#ping"


class PingCommandHandler:
    """Send one passive ``pong`` reply for the exact ``#ping`` command."""

    def __init__(self, *, sender: _GroupMessageSender) -> None:
        self._sender = sender

    async def handle(self, message: InternalMessage) -> PingCommandStatus:
        if not is_ping_command(message):
            return PingCommandStatus.NOT_COMMAND
        if not self._valid_context(message):
            return PingCommandStatus.INVALID_CONTEXT

        group_id = message.context.group_id
        assert group_id is not None
        try:
            await self._sender.send_group_message(
                group_id,
                "pong",
                msg_id=message.platform_message_id,
            )
            logger.info("ping command send succeeded")
            return PingCommandStatus.SUCCEEDED
        except Exception as error:
            logger.warning(
                "ping command send failed error_type=%s",
                type(error).__name__,
            )
            return PingCommandStatus.SEND_FAILED

    @staticmethod
    def _valid_context(message: InternalMessage) -> bool:
        if message.platform != "qq_official":
            return False
        if message.context.sub_type != "GROUP_MESSAGE_CREATE":
            return False
        if not message.context.group_id:
            return False
        if not message.platform_message_id:
            return False
        return True
