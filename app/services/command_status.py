"""Single command-status enum shared by every explicit QQ command handler."""

from __future__ import annotations

from enum import Enum


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


__all__ = ["CommandStatus"]
