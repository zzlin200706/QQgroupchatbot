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

The lifespan logs ``bot.stopped`` once the teardown is committed.
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
    "qq_adapter":    "ready",             # ready | starting | disabled
    "llm_provider":  "ready",             # ready | disabled
    "vision_provider": "disabled"          # ready | disabled
  }
}
```

* ``ready`` — every subsystem answered.  Route traffic here.
* ``starting`` — the lifespan is still wiring up.  A probe is
  expected to return 200; orchestrators that gate on
  ``status==ready`` should wait.
* ``degraded`` — the database probe failed.  The bot stays up
  so it can recover; investigate, do not replace the replica.
* ``stopping`` — the lifespan is tearing down.  Drain the
  replica from the load balancer before sending ``SIGTERM``.

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
| `outbox.recovered` | stale running rows moved to pending | count |
| `outbox.retry` | a row was requeued for retry | outbox_id, interaction, attempts, error_type |
| `outbox.completed` | a row transitioned to completed | outbox_id, interaction, attempts |
| `outbox.failed` | a row hit max_attempts | outbox_id, interaction, attempts, error_type |
| `outbox.pruned_terminal` | terminal rows pruned | count, retention_days |
| `outbox.pruned_raw_events` | raw_events rows pruned | count, retention_days |

Secrets are filtered by the global ``SecretRedactionFilter``
installed at startup; the operator can grep for any credential
and is guaranteed to find no match.

## Crash recovery

The interaction outbox is durable.  After a SIGTERM, SIGKILL or
a hard process crash, a row left in ``running`` is moved back to
``pending`` by ``InteractionOutboxWorker.recover`` on the next
start and re-dispatched.  ``attempts`` is preserved, so a row
that hit ``max_attempts`` and was marked ``failed`` is not
re-dispatched.

To inspect the outbox state:

```python
from app.storage.database import Database
from app.storage.interaction_outbox_repository import InteractionOutboxRepository

db = Database("sqlite+aiosqlite:///./data/qqgroupchatbot.db")
await db.initialize()
repo = InteractionOutboxRepository(db.session_factory)
print(await repo.stats())  # {pending, running, completed, failed}
```

## Backup

The database lives in a single file (``data/qqgroupchatbot.db``).
Because the lifespan forces a WAL checkpoint on startup, the
``.db`` file is self-contained as soon as the process has booted
once.  A safe backup is therefore:

```bash
# Make sure the bot has booted at least once after the most
# recent write.  The startup banner confirms this.
sqlite3 data/qqgroupchatbot.db ".backup /var/backups/qqgroupchatbot-$(date +%F).db"
```

For a hot backup while the bot is running, prefer the SQLite
backup API over ``cp``:

```bash
sqlite3 data/qqgroupchatbot.db ".backup /var/backups/qqgroupchatbot.db"
```

Do not ``cp`` the database file while the bot is writing, unless
the bot has been idle for at least the WAL's auto-checkpoint
interval (default 1000 pages).

## Retention

* ``interaction_outbox_retention_days`` (default 7) — terminal
  outbox rows older than this are deleted on worker startup.
* ``raw_events_retention_days`` (default 30) — raw_events older
  than this are deleted on worker startup, subject to FK safety
  (a raw_event referenced by any ``messages`` row or by any
  pending/running outbox row is preserved).

Both retention sweeps are FK-safe and idempotent.  To inspect
how much data would be deleted by a sweep, run the prune
manually from a Python REPL.

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
