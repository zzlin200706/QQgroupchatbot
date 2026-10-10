# Concurrency & Lifecycle Contract

This document captures the concurrency and shutdown semantics that the
audit established in 2026-08.  Future maintainers should not weaken
any of these without first checking the `tests/` suite still passes
and reviewing the related tests.

## Process-wide invariants

1. **Bounded interaction concurrency.** The `QQOfficialEventProcessor`
   only persists raw/normalized events and creates durable outbox rows.
   `InteractionOutboxWorker` runs at most
   `settings.llm_max_concurrent_interactions` (default 4) concurrent
   command or assistant handlers.

   This is the process-local bound against a flood of group messages
   fanning out into provider calls.  A higher value trades memory and
   provider pressure for latency; a lower value protects provider rate
   limits.

2. **HTTP client ownership.** QQ auth, Gateway discovery, outbound
   sending and the optional image resolver share one `httpx.AsyncClient`
   in the runtime. Text/vision providers own separate clients. Lifespan
   closes all owners; an injected client is not closed by its borrowers,
   and a provider reused for text and vision is closed only once.

3. **Single SQLAlchemy engine.** All repositories share one
   `AsyncEngine`.  SQLite connections run in WAL mode with
   `busy_timeout=5000`.

4. **Token cache expires early.** `QQOfficialAuthClient` caches one
   token until `expires_in - expiry_safety_seconds` has elapsed. The
   sender invalidates the cache on HTTP 401 so the next call refreshes it.

5. **Outbox claim serialisation.** The repository serialises pending
   row claims with an in-process lock.  SQLite still enforces status
   transitions, but this keeps the single-process worker simple.

## Inbound event lifecycle

```text
QQ event
 → adapter (gateway / webhook)
 → QQOfficialInboundEvent
 → QQOfficialEventProcessor.process()
     ├─ raw_ingestion.ingest()    (returns existing row on duplicate)
     ├─ normalized_ingestion.ingest()    (UNIQUE on raw_event_id+parser)
     ├─ interaction_dispatcher.interaction_name()
     └─ interaction_outbox.create_pending()
          → InteractionOutboxWorker.claim_next_pending()
              → interaction_dispatcher.handle()
              → command_dispatcher OR group_assistant_handler
                   → summary_service / ping_command
                   → LLM provider (bounded retry, Retry-After aware)
                   → QQOfficialGroupMessageSender
              → completed / failed
```

## Shutdown sequence

The lifespan `finally` block runs in this order:

1. `gateway_stop.set()` signals the dispatch loop to exit.
2. The dispatch task is cancelled and awaited.
3. The Gateway client is closed (heartbeat task cancelled + socket closed).
4. `outbox_worker.stop(drain_timeout_seconds=...)` stops claiming new
   work, waits briefly for running rows, then cancels leftovers.
   A timeout triggers only a warning.
5. Rows still in `running` are recovered on the next start according
   to their attempt count.
6. The image resource resolver, vision provider, LLM provider, and
   shared HTTP client are closed in that order.
7. The database engine is disposed last.

This sequence is unit-tested in `tests/test_health.py` and
`tests/integration/test_qq_official_concurrency_lifecycle.py`.  When
modifying it, add a regression test that drives the new ordering
through `app.router.lifespan_context`.

## Failure isolation rules

- **Raw ingestion failure** never raises to the gateway loop; the
  process returns a `QQOfficialEventProcessResult(raw_persisted=False)`
  and the loop logs once.
- **Normalized ingestion failure** never raises; the result reports
  `normalized_persisted=False`.  The raw event is always preserved
  (raw-first principle).
- **Interaction failure** is logged with `exc_info` where delivery is
  unconfirmed.  The row is not blindly retried after possible outbound
  delivery.
- **LLM provider errors** raise a typed exception that the handler
  converts into a generation-failed status. Summary failures do not
  persist or send a result. Duplicate claims and configured per-group
  cooldowns are separate outcomes.

## Cancellation rules

- `InteractionOutboxWorker.stop()` is idempotent enough for lifespan
  shutdown and clears its task set before the same worker restarts.
- Worker tasks are cancelled in a single `gather(..., return_exceptions=True)`
  so a single misbehaving task cannot prevent the others from being cancelled.
- `asyncio.CancelledError` propagates through the interaction handler.
  Cancellation before sending prevents a reply; cancellation during an
  HTTP send cannot prove whether QQ accepted it. Such delivery remains
  unconfirmed and must not be blindly retried.
- The single gateway heartbeat task is owned by the Gateway client and
  is always cancelled inside `close()`, regardless of who else holds a
  reference.

## What this contract does NOT cover

- **Delivery certainty across hard crashes.** A hard crash during or
  immediately after outbound delivery can leave a row in `running`.
  The next start may recover it to `pending` only when attempts remain;
  once attempts are exhausted it is marked failed with delivery
  unconfirmed.  Raw event and normalized message facts remain intact.
- **Cross-process coordination.** The bot is single-process by design
  (per AGENTS.md).  No locks are shared with peer processes.
- **Token refresh across long-lived connections.** The token cache
  intentionally lives in-process; on restart the new process fetches a
  fresh token.
