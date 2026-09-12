"""QQ Official orchestration for the exact manual ``#ping`` command."""

from __future__ import annotations

import logging
from typing import Protocol

from app.services.command_status import CommandStatus

from app.domain.messages import InternalMessage, ProvenanceSource, TextSegment


logger = logging.getLogger(__name__)


class _GroupMessageSender(Protocol):
    async def send_group_message(
        self,
        group_id: str,
        message: str,
        *,
        msg_id: str | None = None,
    ) -> object: ...


class _CommandClaimStore(Protocol):
    async def claim(self, **kwargs: str) -> bool: ...


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

    def __init__(
        self,
        *,
        sender: _GroupMessageSender,
        claim_repository: _CommandClaimStore | None = None,
    ) -> None:
        self._sender = sender
        self._claim_repository = claim_repository

    async def handle(self, message: InternalMessage) -> CommandStatus:
        if not is_ping_command(message):
            return CommandStatus.NOT_COMMAND
        if not self._valid_context(message):
            return CommandStatus.INVALID_CONTEXT

        group_id = message.context.group_id
        message_id = message.platform_message_id
        assert group_id is not None
        assert message_id is not None
        if self._claim_repository is not None:
            try:
                claimed = await self._claim_repository.claim(
                    platform=message.platform,
                    group_id=group_id,
                    trigger_message_id=message_id,
                    command_name="ping",
                )
            except Exception as error:
                logger.warning(
                    "ping command claim failed error_type=%s",
                    type(error).__name__,
                )
                return CommandStatus.PERSISTENCE_FAILED
            if not claimed:
                logger.info("duplicate ping command ignored")
                return CommandStatus.DUPLICATE
        try:
            await self._sender.send_group_message(
                group_id,
                "pong",
                msg_id=message_id,
            )
            logger.info("ping command send succeeded")
            return CommandStatus.SUCCEEDED
        except Exception as error:
            logger.warning(
                "ping command send failed error_type=%s",
                type(error).__name__,
            )
            return CommandStatus.SEND_FAILED

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
