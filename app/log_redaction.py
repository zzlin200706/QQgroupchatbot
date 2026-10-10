"""Global secret-redaction filter for application log records.

The bot's own code paths should not log credentials, but a stray
``logger.warning("api_key=%s", key)`` during incident response is a
plausible mistake.  This filter replaces any occurrence of a configured
secret literal inside the formatted log message with ``"***"`` so the
accident does not propagate to disk or to a centralised log
collector.

The filter redacts configured secret literals and observed QQ signed-media
URLs. Other transformed or unconfigured credentials cannot be recognised.
Callers must still avoid passing credentials to ``logger.*`` calls.
"""

from __future__ import annotations

import logging
from app.domain.messages.forward_display import redact_media_urls
from collections.abc import Iterable

_REDACTED = "***"


class SecretRedactionFilter(logging.Filter):
    """Scrub messages and formatted exception/stack details before output."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Keep only non-empty strings; empty literals would be a global
        # search-and-replace and break the whole log stream.
        self._secrets = tuple(
            secret for secret in (str(value) for value in secrets) if secret.strip()
        )

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        message = record.getMessage()
        redacted = self._scrub(message)
        if redacted != message:
            # Replace the formatted message so handlers and formatters
            # cannot recover the original secret through record.args.
            record.msg = redacted
            record.args = ()

        # Formatter appends exceptions separately from getMessage().
        # Cache a scrubbed rendering without changing the exception
        # object that other application code might still reference.
        exception_text = record.exc_text
        if exception_text is None and record.exc_info:
            exception_text = logging.Formatter().formatException(record.exc_info)
        if exception_text is not None:
            scrubbed_exception = self._scrub(exception_text)
            if scrubbed_exception != exception_text:
                record.exc_text = scrubbed_exception
        if record.stack_info:
            record.stack_info = self._scrub(record.stack_info)
        return True

    def _scrub(self, message: str) -> str:
        for secret in self._secrets:
            if secret and secret in message:
                message = message.replace(secret, _REDACTED)
        return redact_media_urls(message)


def install_secret_redaction(
    secrets: Iterable[str],
    *,
    logger_name: str | None = None,
) -> SecretRedactionFilter:
    """Filter the target logger and its currently configured output handlers.

    Ancestor logger filters are not invoked for propagated records, so
    handlers must also filter records originating from ``app.*`` and
    other child loggers.  Install after configuring logging; any handler
    added later must receive the filter too.
    """

    filter_ = SecretRedactionFilter(secrets)
    target = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    target.addFilter(filter_)
    for handler in target.handlers:
        handler.addFilter(filter_)
    return filter_
