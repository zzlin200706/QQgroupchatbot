"""Per-group AI configuration resolution.

The resolver turns a possibly-missing database row into a concrete
``GroupAIConfigSnapshot`` with three-state semantics:

* ``None`` value in the snapshot = "use the global default";
* non-``None`` value = "use this override for the group".

The handler (and any future caller) then combines the snapshot
with the global default to compute the effective setting.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.storage.group_ai_config_repository import GroupAIConfigRepository


@dataclass(frozen=True)
class GroupAIConfigSnapshot:
    """The raw override values for one (platform, group_id) pair.

    Both fields are nullable: ``None`` means "no override; use the
    global default".  The handler is responsible for combining the
    snapshot with the global defaults.
    """

    enabled: bool | None
    summary_max_input_chars: int | None


class GroupAIConfigResolver:
    """Resolve a per-group AI configuration snapshot from the database.

    The resolver is intentionally thin: it reads the override row
    and returns the snapshot, leaving the global-default merge
    logic to the caller.  This keeps the resolver testable in
    isolation and the merge logic explicit at the call site.
    """

    def __init__(self, repository: GroupAIConfigRepository) -> None:
        self._repository = repository

    async def resolve(
        self, *, platform: str, group_id: str
    ) -> GroupAIConfigSnapshot:
        """Return the override snapshot for one (platform, group_id) pair.

        If no row exists, the snapshot is ``(None, None)`` — i.e.
        "use the global default for both knobs".
        """

        record = await self._repository.get(platform, group_id)
        if record is None:
            return GroupAIConfigSnapshot(
                enabled=None, summary_max_input_chars=None
            )
        return GroupAIConfigSnapshot(
            enabled=record.enabled,
            summary_max_input_chars=record.summary_max_input_chars,
        )

    @staticmethod
    def effective_enabled(
        snapshot: GroupAIConfigSnapshot, *, global_enabled: bool
    ) -> bool:
        """Apply the override to a global default and return the effective value."""

        if snapshot.enabled is None:
            return global_enabled
        return snapshot.enabled

    @staticmethod
    def effective_summary_max_input_chars(
        snapshot: GroupAIConfigSnapshot, *, default: int
    ) -> int:
        """Apply the override to the default and return the effective value."""

        if snapshot.summary_max_input_chars is None:
            return default
        return snapshot.summary_max_input_chars
