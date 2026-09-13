# Concurrency & Lifecycle Contract

This document captures the concurrency and shutdown semantics that the
audit established in 2026-08.  Future maintainers should not weaken
any of these without first checking the `tests/` suite still passes
and reviewing the related tests.

## Process-wide invariants

1. **Bounded LLM concurrency.** The `QQOfficialEventProcessor` runs at
   most `settings.llm_max_concurrent_interactions` (default 4)
   concurrent LLM-bound interactions.  Additional work arriving while
   the bound is reached is *refused*, not queued.  The result's
   `interaction_scheduled` field reports this explicitly.

   This is the only mitigation against a flood of group messages
   fanning out into an unbounded number of provider calls.  A higher
   value trades memory for latency; a lower value protects provider
   rate limits.

2. **Single shared HTTP client.** `httpx.AsyncClient` is created once
   during `lifespan` startup and shared between the auth client, the
   Gateway client, the message sender, the LLM provider, and the image
   resource resolver.  Lifespan shutdown is the only place that calls
   `aclose()` on it.

3. **Single SQLAlchemy engine.** All repositories share one
   `AsyncEngine`.  SQLite connections run in WAL mode with
   `busy_timeout=5000`.

4. **Token cache lifetime ≤ expiry_safety_seconds.** The
   `QQOfficialAuthClient` caches one token; the sender invalidates the
   cache when the API returns 401 so the next call forces a refresh.

5. **Single mutex for state changes.** The event processor serialises
   close / scheduling / counter changes through the BoundedSemaphore
   and a plain `bool` (the `_closed` flag).  No other code path mutates
   these.

## Inbound event lifecycle

```text
QQ event
 → adapter (gateway / webhook)
 → QQOfficialInboundEvent
 → QQOfficialEventProcessor.process()
     ├─ raw_ingestion.ingest()    (returns existing row on duplicate)
     ├─ normalized_ingestion.ingest()    (UNIQUE on raw_event_id+parser)
     ├─ interaction_dispatcher.interaction_name()
     └─ _try_schedule_interaction()    (refuses when saturated / closed)
          → asyncio.create_task(_run_interaction())
                → interaction_dispatcher.handle()
                → command_dispatcher OR group_assistant_handler
                     → summary_service / ping_command
                     → LLM provider (bounded retry, Retry-After aware)
                     → QQOfficialGroupMessageSender
```

## Shutdown sequence

The lifespan `finally` block runs in this order:

1. `gateway_stop.set()` signals the dispatch loop to exit.
2. The dispatch task is cancelled and awaited.
3. The Gateway client is closed (heartbeat task cancelled + socket closed).
4. `event_processor.drain(timeout=settings.qq_shutdown_drain_timeout_seconds)`
   waits briefly for already-scheduled interactions.  A timeout
   triggers only a warning, never a raise.
5. `event_processor.aclose()` cancels any tasks that did not finish
   inside the drain window.
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
- **Interaction failure** is logged with `exc_info` so the traceback
  is recoverable.  Other interactions keep running.
- **LLM provider errors** raise a typed exception that the command
  handler converts into a user-visible reply (or a `DUPLICATE` status
  on a 5-minute cooldown).

## Cancellation rules

- `aclose()` is idempotent.  Calling it twice is a no-op.
- Tasks captured in `event_processor._interaction_tasks` are cancelled
  in a single `gather(..., return_exceptions=True)` so a single
  misbehaving task cannot prevent the others from being cancelled.
- A cancelled interaction does **not** send a QQ message; the
  `asyncio.CancelledError` is propagated into the interaction handler,
  which is responsible for translating it into a final user-visible
  state or skipping the send.
- The single gateway heartbeat task is owned by the Gateway client and
  is always cancelled inside `close()`, regardless of who else holds a
  reference.

## What this contract does NOT cover

- **Durability across restarts.** There is no outbox / pending-
  interaction table.  A SIGTERM during an in-flight LLM call loses
  the reply (the `raw_event` is still persisted).  This is a known
  gap; the audit lists it as a deferred feature.
- **Cross-process coordination.** The bot is single-process by design
  (per AGENTS.md).  No locks are shared with peer processes.
- **Token refresh across long-lived connections.** The token cache
  intentionally lives in-process; on restart the new process fetches a
  fresh token.
