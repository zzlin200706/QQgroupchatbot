"""Light-weight structured logging for operational events.

The application uses the standard library ``logging`` module.  This
helper provides a single ``event(name, **fields)`` call that emits
a log line in a stable ``key=value`` shape so an operator (or a log
aggregator like Loki / Datadog) can filter by event name without
parsing free-form sentences.

The set of event names is intentionally small and stable.  A
regression that changes an event name is a behavioural change for
the operator's dashboard and must be called out in the changelog.

Current event names:

  * ``bot.started``              — the lifespan entered
  * ``bot.stopped``              — the lifespan exited cleanly
  * ``bot.startup_failed``       — the lifespan raised before yielding
  * ``qq.adapter_ready``         — gateway connected / webhook ready
  * ``qq.adapter_failed``        — gateway failed
  * ``provider.ready``           — LLM provider constructed
  * ``provider.request_failed``  — provider returned an LLMProviderError
  * ``provider.timeout``         — provider timed out
  * ``image.fetch_failed``       — image download failed
  * ``image.vision_failed``      — vision provider failed
  * ``worker.started``           — outbox worker started
  * ``worker.stopped``           — outbox worker stopped
  * ``worker.task_failed``       — a worker task raised
  * ``outbox.recovered``         — stale running rows moved to pending
  * ``outbox.retry``             — a row was requeued for retry
  * ``outbox.completed``         — a row transitioned to completed
  * ``outbox.failed``            — a row hit max_attempts and is now failed
  * ``outbox.pruned_terminal``   — terminal rows pruned
  * ``outbox.pruned_raw_events`` — raw_events rows pruned
"""

from __future__ import annotations

import logging
from typing import Any

_VALID_LEVELS = {"debug", "info", "warning", "error", "exception"}


def event(name: str, *, level: str = "info", **fields: Any) -> None:
    """Emit a single structured event log line.

    ``fields`` are rendered as ``key=value`` with ``str()`` applied.
    Secrets are filtered by the global ``SecretRedactionFilter``
    installed at startup; the caller does not need to worry about
    accidentally including a credential.
    """

    logger = logging.getLogger("app.events")
    if level not in _VALID_LEVELS:
        level = "info"
    rendered_fields = " ".join(
        f"{key}={_render_value(value)}" for key, value in fields.items()
    )
    message = f"event={name} {rendered_fields}".rstrip()
    log_fn = getattr(logger, level)
    log_fn(message)


def _render_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    # Collapse embedded whitespace so the line is one log record.
    return " ".join(text.split())
