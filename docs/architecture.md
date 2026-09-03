# Architecture

## The shape of the thing

```
        CLI (Typer)                    FastAPI (/api/v1)
             │                                │
             └──────────────┬─────────────────┘
                            ▼
                        Services
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
 TrackingEngine        RuleEngine       NotificationService
        │                   │                   │
        ▼                   │                   ▼
  StoreAdapter ◄────────────┘          NotificationProvider
        │                                       │
        ▼                                       ▼
  E-commerce sites                  Console · Email · Telegram · Webhook
        │
        ▼
   Repositories ──────────► PostgreSQL
                                ▲
                                │
                    Scheduler / Background Worker
```

Dependencies point inward. `api`, `cli`, and `scheduler` depend on `services`; `services`
depend on `repositories` and on *interfaces*; `domain` depends on nothing at all.

The property that makes this extensible is one import rule: **the tracking engine imports
no concrete store and no concrete notification provider.** It holds a `StoreRegistry` and a
`NotificationService`, both of which resolve implementations at runtime. That is why adding
Amazon or Slack is a new file rather than an edit to the core.

## Layers

| Layer | Package | Rule |
|---|---|---|
| Domain | `domain/` | Enums, frozen value objects, exceptions, protocols. Imports nothing from the app. |
| Persistence | `db/`, `repositories/` | SQLAlchemy only. Repositories hold no business rules and never commit — the caller owns the transaction. |
| Services | `services/` | All the decisions. Talks to repositories and to interfaces. |
| Plugins | `stores/`, `notifications/` | Implement an interface. Registered, never imported by the engine. |
| Delivery | `api/`, `cli/`, `scheduler/`, `workers/` | Translate the outside world into service calls. |
| Web UI | `frontend/` (React SPA) + `web/` (serves it) | A pure client of `/api/v1`. No server-side rendering, no business logic. |

The web UI is deliberately outside the Python process's logic: `frontend/` builds to static
files, `web/serve.py` mounts them at `/ui`, and the browser calls `/api/v1` like any other
client. The eight-state listing resolver and the retailer-field rules live in TypeScript
with a Vitest suite; the Python side only checks the bundle is served. Keeping the UI a
separate build means it can grow — or be replaced — without touching the API.

### Why `domain/models.py` and `db/models.py` both exist

The first is pure value objects; the second is SQLAlchemy. Merging them would mean a rule
evaluator holding a live ORM object, and one careless attribute access would emit a query
from inside what is supposed to be a pure function. `ProductSnapshot` exists precisely so
evaluators cannot touch the session.

## The decisions worth knowing

### Availability is independent of extraction success

The single most important invariant. `FetchResult` carries `outcome` *and* `availability`,
and a failure to read a price sets availability to `UNKNOWN` — never `OUT_OF_STOCK`.

A price tracker that reports "out of stock" when its parser broke will send false alerts
and destroy the user's trust in every alert after that. So `PRICE_NOT_FOUND` is a distinct
outcome from `OUT_OF_STOCK`, `check_executions` records which one happened, and history is
only written for what was actually learned.

### A price has a location, and we say when we could not set it

Indian retailers price and stock per delivery area, so every price this tracker has ever
recorded came from *some* area — until `DELIVERY_PINCODE` existed, an unstated one.

`stores/pincode.py` is the single place that knows what each host does with a PIN code.
Adapters never read `ctx.delivery_pincode`: they call `apply` on the way out and
`escalate` on the way back. Today every classified host is `needs_js` — no shop takes a
delivery area in a plain cookie or query parameter, and reproducing the session
handshakes that do work would be anti-bot evasion. So `apply` is a no-op and the value is
in `escalate`, which turns a `PRICE_NOT_FOUND` on an area-priced shop into
`NEEDS_LOCATION`.

That is a deliberately narrow claim, and it only ever *narrows*: a successful read is
untouched, a block stays a block, and availability is carried through unchanged. The
alternative — recording the default area's price as the answer — is the same class of
error as calling a failed extraction "out of stock", and it is harder to notice because
the number looks right.

`NEEDS_LOCATION` maps to `CheckStatus.PARTIAL`, not `FAILED`: the request was fine and
the page was readable. In the comparison grid it lands as `NO_PRICE`.

### History records observations; executions record attempts

Two tables, two purposes:

- `price_history` and `availability_history` are **append-only** and hold only *meaningful*
  observations: the first, and each change. Nothing updates or deletes them.
- `check_executions` records **every attempt**, successful or not, with the reason, the
  method, the HTTP status, and the duration.

So "what did this cost last month?" and "why did this fail at 3am?" are different queries
against different tables, and neither pollutes the other.

### The engine fetches outside the transaction

A check can take 25 seconds. Holding a PostgreSQL transaction open across it would pin a
connection and block autovacuum for no benefit. The fetch happens first, then all writes
land in one short transaction.

### Rules are rows, not branches

A `RuleType` maps to an evaluator: a pure function of `(rule, RuleContext)`. Rule-specific
settings live in a JSONB `params` column, so a new condition with new options needs no
migration. Adding a condition touches two files and nothing else.

### Notifications are recorded, then delivered

Two steps, in **two transactions**. The row exists — guarded by a unique `dedupe_key` —
and is committed before anything is sent, so a crash between deciding and sending leaves a
pending row rather than a silent loss. `INSERT … ON CONFLICT DO NOTHING` means the
*database* enforces single delivery, not a check-then-insert that a race could slip between.

Delivery runs in its own transaction (`services/check_runner.py`) because it talks to SMTP
servers and webhooks: holding the check's transaction across that would pin a connection
for as long as the slowest provider takes.

### Worker liveness is measured, not inferred

The worker upserts a heartbeat row on every reconcile and deletes it on clean shutdown, so
any process can read a direct answer to "is a worker running?". Inferring it from overdue
jobs — the earlier approach — could not tell "nothing is running" from "a worker is running
but wedged mid-check", and cried wolf whenever a check legitimately ran long.

Two workers appear as two rows, and `status` warns: they have no cross-process locking, so
every job would run twice.

### Scheduling hides behind an interface

`JobQueue` is the abstraction; APScheduler with a PostgreSQL job store is the only
implementation. Job ids are deterministic (`product:42`), so scheduling a product twice
replaces its job instead of adding a second. Celery would be one more implementation, with
nothing else to change.

### Product Entries sit above listings, not beside them

```
ProductEntry            the identity a person keeps
    |
    +-- RetailerListing     one per shop: their name for it, their URL, active or not
            |
            +-- Product         the tracking target, shared between users
                    |
                    +-- price_history / availability_history / check_executions
```

`RetailerListing` is a thin layer over `products`, not a replacement for it. The product
row stays the thing the engine checks and the thing history hangs off; the listing carries
what belongs to one user's entry. Its foreign key to `products` is `RESTRICT`, so removing
a listing can never cascade into a shared product's observations.

The partial unique index `(product_entry_id, store_slug) WHERE deactivated_at IS NULL`
allows at most one live listing per shop per entry while letting a deactivated one sit
beside its replacement — which is exactly what re-pointing a URL leaves behind.

Multi-user arrived with `users` and `subscriptions` (migration 0007): listings are shared
rows, and who watches what is a subscription. Per-store pacing is shared across processes
through `store_pacing` (migration 0010), so two people searching at once queue behind the
same slots rather than acting as two independent rate limiters.

## Data model

```
stores ──< products ──< price_history
                    ├──< availability_history
                    ├──< tracking_rules ──< notifications
                    └──< check_executions
```

- `products.url_canonical` is unique — the same listing shared with different tracking
  parameters is recognised as a duplicate rather than tracked twice.
- `notifications.dedupe_key` is unique — this is what makes alerting idempotent.
- Every history row carries `check_execution_id`, so any recorded price traces back to the
  fetch that saw it.
- Deleting a product cascades to everything below it.

## Request and check flow

**Adding a product** — validate URL (scheme, no credentials, length) → SSRF guard →
canonicalise → resolve adapter → duplicate check → insert.

**Checking a product** — guard (throttle, circuit breaker) → resolve adapter → fetch, with
retries on transient outcomes only → detect change → append history → evaluate rules →
record notifications → deliver → write the execution row.

Nothing in the second half can fail the check. The observation is stored before rules run,
and a misbehaving rule or an unreachable provider is logged and recorded, not raised.

## What is deliberately not here

- **Celery/Redis.** APScheduler with a Postgres job store covers one worker; `JobQueue`
  makes the swap cheap when it is actually needed.
- **A shared *API* rate limit.** The API's per-client limiter is per-process and
  in-memory; two API processes each enforce their own. (Per-*store* pacing *is* shared —
  see `store_pacing` below.)
- **Anti-bot evasion.** Blocks are recorded and respected. No CAPTCHA solving, no
  fingerprint spoofing, no credential use.
