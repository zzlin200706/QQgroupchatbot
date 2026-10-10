# Operations Guide (v1.0)

This document captures the operational contract for the v1.0
release.  It is intended for the operator who runs the bot on a
long-lived host (a VM, a container, a systemd unit) and needs to
audit health, debug problems, and back up state.

## Startup

On boot the lifespan logs a single ``bot.started`` event with the
enabled feature flags and the effective transport.  The first
``/health`` request before the worker has finished its startup
returns ``status=starting``; once recovery and pruning are done
the state flips to ``ready``.  An orchestrator probe should treat
``starting`` and ``ready`` as healthy.

## Shutdown

The lifespan logs ``bot.stopped`` when teardown begins.
The teardown is bounded: the outbox worker is given
``qq_shutdown_drain_timeout_seconds`` (default 5s) to drain
in-flight rows.  Rows still in ``running`` at that point are
recovered on the next start; the bot does not lose work to a
clean shutdown.

## Health

```http
GET /health
200 OK
{
  "status": "ready",                       # starting | ready | degraded | stopping
  "app": "qqgroupchatbot",
  "environment": "production",
  "checks": {
    "database":      "ok",                 # ok | degraded
    "qq_adapter":    "ready",             # ready | starting | degraded
    "outbox_worker": "ready",             # ready | starting | degraded
    "llm_provider":  "ready",             # ready | disabled
    "vision_provider": "disabled"          # ready | disabled
  }
}
```

* ``ready`` — the database, adapter and outbox worker are healthy. Route traffic here.
* ``starting`` — the lifespan is still wiring up.  A probe is
  expected to return 200; orchestrators that gate on
  ``status==ready`` should wait.
* ``degraded`` — the database probe, gateway connection or outbox
  worker failed.  The bot stays up so it can recover; investigate.
* ``stopping`` — the lifespan is tearing down.  Drain the
  replica from the load balancer before sending ``SIGTERM``.

The provider checks report whether the provider object was configured
and constructed.  They do not perform a network health probe.

## Structured events

The application emits a small, fixed set of event names on the
``app.events`` logger in ``key=value`` form.  An operator (or a log
aggregator) can filter by event name without parsing free-form
sentences.

| Event | When | Fields |
|---|---|---|
| `bot.started` | lifespan enters | app_env, transport, llm_provider, summary_command_enabled, group_assistant_enabled, vision_enabled |
| `bot.stopped` | lifespan exits cleanly | app_env |
| `qq.adapter_ready` | gateway connected | transport |
| `qq.adapter_failed` | gateway raised | stage, error_type, (transport, event_type) |
| `provider.ready` | LLM provider constructed | provider, model, vision_model |
| `provider.request_failed` | LLM returned a typed error | provider, model, status, error_type |
| `provider.timeout` | LLM timed out | provider, model, status |
| `image.fetch_failed` | image download failed | position, error_type |
| `image.vision_failed` | vision provider failed | position, error_type |
| `worker.started` | outbox worker started | num_workers, max_concurrent |
| `worker.stopped` | outbox worker stopped | num_workers |
| `outbox.recovered` | stale running rows moved to pending or failed when attempts are exhausted | count |
| `outbox.completed` | a row transitioned to completed | outbox_id, interaction, attempts |
| `outbox.failed` | a row transitioned to failed | outbox_id, interaction, attempts, error_type |
| `outbox.pruned_terminal` | terminal rows pruned | count, retention_days |

Configured credentials and recognized token patterns are filtered by
``SecretRedactionFilter`` on logging handlers installed at startup,
including propagated records and formatted exceptions. Avoid logging
raw payloads or credentials; filtering does not replace that rule.

## Crash recovery

The interaction outbox is durable.  After a SIGTERM, SIGKILL or
a hard process crash, a row left in ``running`` is recovered on
the next start.  Rows that have attempts remaining move back to
``pending``.  Rows that reached ``max_attempts`` are marked
``failed`` because delivery is unconfirmed and the bot must not
blindly call the LLM or send again.

To inspect the outbox state:

```python
from app.storage.database import Database
from app.storage.interaction_outbox_repository import InteractionOutboxRepository

db = Database("sqlite+aiosqlite:///./data/qqgroupchatbot.db")
await db.initialize()
repo = InteractionOutboxRepository(db.session_factory)
print(await repo.stats())  # {pending, running, completed, failed}
```

## Outbound delivery confirmation

An HTTP 2xx status alone is not a successful group-message receipt.
The sender checks the documented `err_code` (and the previously supported
`code` / `error.code` envelopes), then requires a non-empty message `id`.
Nonzero business codes fail even if an `id` is also present. Error codes
and descriptions are redacted before being attached to exceptions.

HTTP 201/202 indicate asynchronous acceptance, not delivery confirmed by
this application. Until an asynchronous receipt reconciler exists, the
sender raises an unconfirmed-response error. The command reports
`send_failed`, and the outbox becomes terminal `failed`; this includes
unknown delivery, not only confirmed non-delivery. A generated summary
stays in `summaries`. Do not rerun the model, clear the durable claim, or
manually requeue the send: a delayed platform delivery may still occur.

## Backup

The database defaults to ``data/qqgroupchatbot.db``. While it is open,
committed writes may also reside in its WAL sidecar. Use the SQLite
backup API to take a consistent snapshot, including while the bot runs:

```bash
sqlite3 data/qqgroupchatbot.db ".backup /var/backups/qqgroupchatbot-$(date +%F).db"
```

Verify a backup before relying on it (expected output: ``ok``):

```bash
sqlite3 /var/backups/qqgroupchatbot-YYYY-MM-DD.db "PRAGMA integrity_check;"
```

Do not copy only the live ``.db`` file. Idle time or a startup checkpoint
does not prove that the WAL contains no subsequent writes. Retain backups
of all raw receipts as well as the normalized tables.

## Long summary windows

``SUMMARY_MAX_INPUT_CHARS`` bounds each user prompt, including the time
range, message count, and data delimiters. The fixed system prompt is
additional. Long windows are split at message boundaries, with every
chunk checked before text generation starts. A single oversized message
raises ``SummaryWindowTooLarge``; narrow the time window or increase the
budget to fit the configured model.

Synthesis receives all validated fields, including decisions, action
items, owners, deadlines and open questions. If that prompt exceeds the
budget, the service concatenates summaries and deduplicates structured
fields without another model call (``finish_reason=deterministic_merge``).
Conflicting statements remain present for review. Invalid or truncated
model output fails the whole generation: raw and normalized messages
remain, but no summary is persisted or sent.

Prompt version ``3`` adds readable, unverified forwarded-content rules and
continues to record the original rendered conversation length
in ``input_chars`` and sums token usage across all generation calls.
If any call omits a usage field, that aggregate stays unknown (``null``).

## Retention

For parser v1 forward history, run
`.venv/bin/python -m scripts.reparse_qq_official_forwards` to preview, then
add `--apply` to append the current parser representation. Optional
`--raw-event-id ID` limits scope. This tool requires an existing SQLite
database; it never starts the bot or invokes providers/commands. Old
normalized versions and raw receipts are preserved. Normal conversation
queries choose the newest representation without duplicating history.
Restart a running bot to load the parser/renderer/prompt changes.

* ``interaction_outbox_retention_days`` (default 7) — terminal
  outbox rows older than this are deleted on worker startup.
* Raw events are retained permanently.  ``raw_events_retention_days``
  is kept as a legacy setting and ignored by runtime startup.

The terminal outbox sweep is idempotent.  Raw-event pruning helpers
remain test-covered for manual maintenance, but normal bot startup
does not invoke them.

## Logs

Logs go to stderr.  The operator's responsibility is to ship
them to a log aggregator (Loki, Datadog, etc.) if desired.  The
bot does not own its own log file.

## Configuration

See ``Settings.audit()`` for a secret-free snapshot of the
effective configuration.  The lifespan startup banner does not
log the audit directly; an operator can call it from a Python
REPL:

```python
from app.config import get_settings
print(get_settings().audit())
```

Empty ``*_model`` and ``<unset>`` markers indicate an operator
forgot to set the corresponding env var; the validator refuses
to start the bot in that case.
