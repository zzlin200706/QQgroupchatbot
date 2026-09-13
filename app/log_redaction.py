"""Global secret-redaction filter for application log records.

The bot's own code paths should not log credentials, but a stray
``logger.warning("api_key=%s", key)`` during incident response is a
plausible mistake.  This filter replaces any occurrence of a configured
secret literal inside the formatted log message with ``"***"`` so the
accident does not propagate to disk or to a centralised log
collector.

The filter only redacts *literal* secrets; it cannot catch an
attacker-supplied piece of text that happens to contain the same
bytes.  The real defence is to never pass secrets through
``logger.*`` calls in the first place.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

_REDACTED = "***"


class SecretRedactionFilter(logging.Filter):
    """Scrub configured secret literals from every log record's message."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Keep only non-empty strings; empty literals would be a global
        # search-and-replace and break the whole log stream.
        self._secrets = tuple(
            secret for secret in (str(value) for value in secrets) if secret
        )

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = self._scrub(message)
        if redacted is message:
            return True
        # Replace the formatted message so the rest of the logging
        # machinery (handlers, formatters) sees the scrubbed text.
        record.msg = redacted
        record.args = ()
        return True

    def _scrub(self, message: str) -> str:
        for secret in self._secrets:
            if secret and secret in message:
                message = message.replace(secret, _REDACTED)
        return message


def install_secret_redaction(
    secrets: Iterable[str],
    *,
    logger_name: str | None = None,
) -> SecretRedactionFilter:
    """Attach a :class:`SecretRedactionFilter` to the named logger (or root)."""

    filter_ = SecretRedactionFilter(secrets)
    target = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    target.addFilter(filter_)
    return filter_
