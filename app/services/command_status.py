"""Single command-status enum shared by every explicit QQ command handler."""

from __future__ import annotations

from enum import Enum

from app.domain.messages import InternalMessage


class CommandStatus(str, Enum):
    """Outcome of one inbound command evaluation.

    DISABLED:               handler is configured off
    NOT_COMMAND:            message did not match the command's exact text
    INVALID_CONTEXT:        required group / message-id / tz-aware timestamp missing
    DUPLICATE:              command_trigger_claims already recorded this message id
    COOLDOWN:               per-group cooldown window not yet elapsed
    IN_PROGRESS:            a previous invocation for the same (platform, group)
                            is still running in this process
    GENERATION_FAILED:      LLM provider raised before any persistence
    PERSISTENCE_FAILED:     claim insert, summary persist or interaction persist
                            raised before the outbound send
    SEND_FAILED:            QQ passive reply raised after a successful persist
    SUCCEEDED:              outbound send completed
    """

    DISABLED = "disabled"
    NOT_COMMAND = "not_command"
    INVALID_CONTEXT = "invalid_context"
    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"
    IN_PROGRESS = "in_progress"
    GENERATION_FAILED = "generation_failed"
    PERSISTENCE_FAILED = "persistence_failed"
    SEND_FAILED = "send_failed"
    SUCCEEDED = "succeeded"


__all__ = ["CommandStatus", "is_valid_command_context"]


def is_valid_command_context(message: InternalMessage) -> bool:
    """Return whether a normalized inbound event is eligible for a command.

    Shared between PingCommandHandler and SummaryCommandHandler so that
    the gate for "is this even a runnable command context" stays in one
    place.  Summary additionally requires a tz-aware timestamp for
    timedelta arithmetic; ping does not consume the timestamp, but the
    strict check is harmless and keeps invariants uniform.
    """
    if message.platform != "qq_official":
        return False
    if message.context.sub_type != "GROUP_MESSAGE_CREATE":
        return False
    if not message.context.group_id:
        return False
    if not message.platform_message_id:
        return False
    timestamp = message.timestamp
    if timestamp is None or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return False
    return True

