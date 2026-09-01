# Product Tracker

Track product prices and availability across e-commerce sites. Give it a product URL; it
identifies the store, extracts the price and stock state, records history, evaluates alert
rules, and notifies you through configured providers — on a schedule, in the background.

Built to be extended: adding a store, a notification channel, or an alert condition means
adding a module, not editing the tracking engine.

> **Status: phases 1–6 of 7 complete.** Feature-complete: add products by URL, a
> background worker checks them on their own interval, records history, and alerts
> you — through an authenticated REST API or the CLI. Phase 7 is the quality pass —
> see [Roadmap](#roadmap).

---

## 1. What this project does

- **Track by URL.** Paste a product link from any supported site. A generic schema.org
  adapter handles sites that publish structured data; named adapters add tuned extraction.
- **Price and availability, separately.** A failure to read a price is recorded as
  "unknown", never as "out of stock". Extraction failures and stock states are different
  facts and are stored as such.
- **Full history.** Every meaningful price observation is appended, never overwritten.
  "Meaningful" means the first observation, a changed price, or a currency switch —
  repeating an unchanged number would grow the series without adding information. Every
  check is still recorded in `check_executions` either way. Availability is stored as one
  row per *transition*. Answers current/lowest/highest/average, when the low occurred, and
  change over time.
- **Configurable alerts.** Rules ("price dropped", "below ₹69,999", "back in stock") are
  rows, not code branches. Six conditions ship; adding one is an evaluator function.
  Notifications are deduplicated by a unique key in the database, so the same alert reaches
  you once however many times it is observed or retried.
- **Runs itself.** A worker process checks products on a per-product interval, retries
  transient failures, throttles requests per store, and records every attempt.
- **Two interfaces.** A Typer CLI for humans and a versioned FastAPI for a future web or
  mobile frontend.

**What it does not do:** bypass CAPTCHAs, authentication, or any access control. When a
store cannot be read, the check is recorded as failed with the reason. It never invents a
price.

### Verified against live retailers

Checked against a real product (Apple iPhone 17, 256 GB) on 2026-09-01, over plain HTTP
with no browser:

| Retailer | Result | Extraction path |
|---|---|---|
| Flipkart | ✅ ₹82,900 · in stock | schema.org JSON-LD |
| Vijay Sales | ✅ ₹82,900 · in stock | generic adapter |
| Reliance Digital | ✅ ₹82,900 · in stock | generic adapter |
| BigBasket | ✅ ₹82,900 · availability *unknown* | labelled text |
| Croma | ❌ `blocked` (HTTP 403) | — |

Notes on the two that are not a plain ✅:

- **BigBasket publishes no availability signal at all** — no JSON-LD, no OpenGraph, and the
  only hint is an "Add to Basket" string, which is present on out-of-stock pages too. The
  honest answer is `unknown`, so that is what is recorded.
- **Croma blocks automated access at the edge.** It returns a 326-byte "Access Denied" page
  to a real headless Chromium as well as to plain HTTP, so this is not a user-agent filter
  and the browser fallback does not help. Getting past it would require IP rotation or
  fingerprint spoofing; this project does not do that. Croma is recorded as `blocked` and
  its circuit breaker backs it off.

## 2. Architecture

```
          CLI (Typer)            FastAPI (/api/v1)
                \                    /
                 +--> Services <----+
                      |      |
         Tracking Engine    Rule Engine
              |                  |
      Store Adapters      Notification Providers
              |                  |
       E-commerce sites     Email / Telegram / Webhook
              |
        Repositories --> PostgreSQL (price history)
                              ^
                              |
                  Scheduler / Background Worker
```

Dependencies point inward. `api`, `cli`, and `scheduler` depend on `services`; `services`
depend on `repositories` and on *interfaces* (`StoreAdapter`, `NotificationProvider`,
`RuleEvaluator`, `JobQueue`); `domain` depends on nothing.

The tracking engine never imports a concrete store or provider. That is the property that
makes the system extensible, and it is enforced by the direction of imports.

Four extension points:

| Interface | Location | Add one by |
|---|---|---|
| `StoreAdapter` | `stores/base.py` | New module in `stores/`, register it, add a catalogue entry |
| `NotificationProvider` | `notifications/base.py` | New module in `notifications/`, register it |
| `RuleEvaluator` | `services/rules_engine.py` | New evaluator, register it against a `RuleType` |
| `JobQueue` | `scheduler/jobqueue.py` | New implementation (e.g. Celery) — engine untouched |

### Deliberate deviations from a plain layered layout

1. **One installable package** (`src/product_tracker/`) rather than bare `src/api`,
   `src/core`, … — no `sys.path` juggling, no top-level name collisions.
2. **`domain/models.py` vs `db/models.py`.** The first is pure value objects; the second is
   SQLAlchemy. Keeping a separate top-level `models/` would blur exactly the boundary the
   design depends on.
3. **No Redis/Celery in v1.** Scheduling sits behind a `JobQueue` interface with an
   APScheduler implementation that persists jobs in PostgreSQL. Celery becomes a drop-in
   implementation when horizontal scale is actually needed. `REDIS_URL` is reserved but
   unused.

## 3. Project structure

```
src/product_tracker/
  core/          Settings (pydantic-settings), structured logging, security helpers
  domain/        Enums, frozen value objects, exception hierarchy. Imports nothing.
  db/            Declarative base, ORM models, engine/session management
  repositories/  Data access, one class per aggregate. Never commits.
  stores/        StoreAdapter interface, registry, per-store adapters, selector configs
  notifications/ NotificationProvider interface, registry, per-channel providers
  services/      Tracking engine, rule engine, statistics, change detection
  scheduler/     JobQueue interface, APScheduler implementation, throttling
  workers/       Entrypoints invoked by scheduled jobs
  api/           FastAPI app factory, routers, schemas, dependencies, error envelope
  cli/           Typer commands
  utils/         URL validation/SSRF guard, money parsing
migrations/      Alembic environment and versions
docker/          Dockerfile, docker-compose.yml, entrypoint
tests/           unit/ (no I/O), integration/ (real PostgreSQL), fixtures/ (saved pages)
```

## 4. Requirements

- **Python 3.12+**
- **PostgreSQL 14+** (16 recommended; compose provides it)
- **Docker + Docker Compose** — optional, but the easiest way to get PostgreSQL
- **Playwright + Chromium** — optional, only for sites needing JavaScript rendering

## 5. Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"                # add ,browser for Playwright support
Copy-Item .env.example .env            # then edit .env
```

With the browser extra:

```powershell
pip install -e ".[dev,browser]"
playwright install chromium
```

## 6. Environment variables

Every setting is read from the environment or `.env`. Nothing is hard-coded and no
credential is committed. `.env.example` is the full annotated list; the ones that matter
most:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | *(required)* | PostgreSQL DSN. `postgres://` and `postgresql://` are normalised onto psycopg 3. |
| `TEST_DATABASE_URL` | — | Throwaway database for integration tests. Unset ⇒ those tests skip. |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Use `console` locally for readable output. |
| `CHECK_INTERVAL_SECONDS` | `3600` | Default per-product interval. Floor of 60s. |
| `HTTP_TIMEOUT_SECONDS` / `HTTP_MAX_RETRIES` | `25` / `3` | Outbound request bounds. |
| `STORE_MIN_INTERVAL_SECONDS` / `FETCH_JITTER_SECONDS` | `5` / `3` | Politeness: minimum gap between requests to one store, plus random jitter. |
| `PLAYWRIGHT_ENABLED` | `true` | Set `false` to run without a browser. |
| `API_KEY` | *(unset)* | If set, required as `X-API-Key` on mutating endpoints. Unset means the API is open — fine on localhost, not in public. |
| `API_ALLOW_ANONYMOUS_READS` | `true` | Set `false` to require the key on `GET` too. |
| `API_MAX_REQUEST_BYTES` | `64000` | Bodies over this are rejected with 413. |
| `BLOCK_PRIVATE_ADDRESSES` | `true` | SSRF guard. Rejects URLs resolving to private/loopback ranges. |
| `NOTIFY_DEFAULT_PROVIDERS` | `console` | Comma-separated provider slugs. |

Secrets (`SMTP_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `API_KEY`) are held as `SecretStr`, masked
by `product-tracker config`, and stripped from logs by a redaction processor.

## 7. Database setup

Start PostgreSQL and apply migrations:

```powershell
docker compose -f docker/docker-compose.yml up -d db
alembic upgrade head
product-tracker stores sync      # populate the store catalogue
product-tracker status           # verify
```

Without Docker, create the database yourself and point `DATABASE_URL` at it:

```sql
CREATE USER tracker WITH PASSWORD 'choose-your-own';
CREATE DATABASE tracker OWNER tracker;
CREATE DATABASE tracker_test OWNER tracker;   -- for the test suite
```

Migrations are hand-written where it matters (the six shared enum types are created once,
not once per column). `alembic downgrade base` is tested and reversible.

## 8. Running the API

```powershell
uvicorn product_tracker.api.app:get_app --factory --reload
```

- `GET /health` — liveness. Never touches the database, so a database outage does not get
  the process restarted.
- `GET /health/ready` — readiness. Reports each dependency; 503 when one is down. An
  unmigrated database counts as not ready.
- `GET /docs` — OpenAPI UI. `GET /openapi.json` — schema.
- `/api/v1/products` — `POST` to track, `GET` to list (paginated, filterable by
  `store` and `tracking_status`).
- `/api/v1/products/{id}` — `GET` one, `DELETE` to stop tracking.
- `/api/v1/products/{id}/check` — `POST` to check now. Returns **200 with a failed
  execution** when a store cannot be read: that is a recorded fact, not a broken API.
- `/api/v1/products/{id}/history` — recorded prices, newest first, paginated.
- `/api/v1/products/{id}/availability` — availability transitions.
- `/api/v1/products/{id}/stats` — current/lowest/highest/average, when the low occurred,
  and change since the first observation. `null` when nothing has been recorded yet.
- `/api/v1/alerts` — `POST` to create a rule, `GET` to list (filter by `product_id`).
- `/api/v1/alerts/{id}` — `GET` one, `DELETE` to remove.
- `/api/v1/products/{id}/pause` and `/resume` — stop or restart scheduled checks.
- `/api/v1/stores` — supported stores, from the adapter registry.

`GET /health/ready` reports four dependencies: `database`, `scheduler`,
`notifications`, and `auth`. Only the database gates readiness — an API that can serve
reads and accept products is doing its job even with no worker running, and failing the
probe would pull it out of the load balancer over a background problem.

### Authentication

Off by default, which is correct for something bound to localhost. Set `API_KEY` and every
mutating endpoint requires the header:

```
X-API-Key: <your key>
```

Reads stay anonymous unless you also set `API_ALLOW_ANONYMOUS_READS=false`. The key is
compared in constant time, a 401 advertises the scheme in `WWW-Authenticate`, and the
health probes never require a credential — a probe should not need one.

Every error response uses one envelope:

```json
{ "error": { "type": "not_found", "message": "Product 42 not found", "detail": null } }
```

## 9. Running the CLI

```powershell
product-tracker --help
product-tracker status          # config + database + tracking state
product-tracker config          # effective settings, secrets redacted
product-tracker stores list
product-tracker stores sync
```

Exit codes: `0` success · `1` unexpected error · `2` not found · `3` store failure ·
`4` configuration error.

```powershell
product-tracker add <URL> [--interval 3600] [--no-check]
product-tracker list [--store flipkart] [--status active] [--limit 20]
product-tracker show <ID>
product-tracker check <ID>
product-tracker remove <ID> [--yes]
product-tracker history <ID> [--stats] [--availability] [--limit 20]

product-tracker alerts add <ID> --type price_dropped
product-tracker alerts add <ID> --type price_below_target --target 69999 [--cooldown 3600]
product-tracker alerts list [--product <ID>]
product-tracker alerts remove <RULE_ID> [--yes]
product-tracker alerts history <ID>
product-tracker pause <ID>
product-tracker resume <ID>

product-tracker worker [--dry-run]
product-tracker check-all [--limit 100]
```

## 10. Running the scheduler/workers

The worker is its own process, separate from the API — a check can take half a minute and
the API should stay responsive.

```powershell
product-tracker worker              # runs until Ctrl+C
product-tracker worker --dry-run    # show what would be scheduled, then exit
product-tracker check-all           # check every active product once, now
# or: docker compose -f docker/docker-compose.yml --profile worker up
```

On Windows, `scripts/install-scheduled-task.ps1` registers it to start at sign-in and
restart if it exits (`-Remove` to undo).

What it does:

- **Persists jobs in PostgreSQL** with a deterministic id per product (`product:42`), so a
  restart resumes the existing schedule instead of re-checking everything at once, and
  scheduling the same product twice replaces its job rather than adding a second.
- **Reconciles against the database** every `RECONCILE_INTERVAL_SECONDS`. Products are
  added and paused through the API and CLI, which never talk to the worker; reconcile is
  what closes that gap.
- **Retries transient failures** (timeouts, 5xx) with capped exponential backoff. A block,
  a missing price, or an unrecognised page is *not* retried — none of those change on an
  immediate second attempt, and retrying a block is what the store is objecting to.
- **Throttles per store**, with jitter, and opens a **circuit breaker** after
  `STORE_FAILURE_THRESHOLD` consecutive failures. Both are per-store, so one failing site
  never delays products from another. A check skipped by the breaker is recorded as
  `skipped` — not `failed`, because nothing was attempted.

> **One worker per database.** APScheduler's job store has no cross-process locking, so a
> second worker would run every job a second time.

## 11. Adding a new store adapter

1. Add a `StoreInfo` entry to `stores/catalogue.py` (slug, display name, domains, adapter key).
2. Create `stores/<slug>.py` implementing `StoreAdapter`: `can_handle_url` and
   `fetch_product`.
3. Keep CSS/XPath selectors in `stores/selectors/<slug>.yaml`, not in the code.
4. Register the adapter in `stores/registry.py`.
5. Save a real page to `tests/fixtures/` and write extraction tests against the file. Tests
   must never hit the live site.
6. `product-tracker stores sync`.

The tracking engine needs no change. Return `FetchOutcome.PRICE_NOT_FOUND` when a price is
missing and leave availability `UNKNOWN` — never report out-of-stock because parsing failed.

## 12. Adding a new notification provider

1. Create `notifications/<slug>.py` implementing `NotificationProvider`: `is_configured`
   and `send`.
2. Add its settings to `core/config.py` (secrets as `SecretStr`) and `.env.example`.
3. Register it in `notifications/registry.py`.
4. Add it to `NOTIFY_DEFAULT_PROVIDERS`.

Raise `NotificationDeliveryError` on failure; the service records it and retries within
bounds. Providers never know why a notification exists.

### Alert conditions and deduplication

Six conditions ship: `price_changed`, `price_dropped`, `price_increased`,
`price_below_target`, `became_available`, `became_unavailable`.

Two deliberate decisions worth knowing:

- **`became_available` requires a known-unavailable previous state.** Coming from
  `unknown` does not fire it — we never established the product *was* unavailable, so
  announcing that it came back would be an invention.
- **`price_below_target` fires on the state, not on crossing.** Setting a target after a
  drop you missed still alerts you. Repetition is handled by deduplication, not by
  narrowing the condition.

A notification's identity is `(product, rule, event, signature, UTC date)`, hashed into the
unique `dedupe_key` column. The signature depends on how the rule fires: change rules key
on the transition (`100->90`), state rules on the resulting state. So the same alert on one
day is delivered once, the same transition next week alerts again, and a rule's
`cooldown_seconds` gives finer control.

## 13. Adding a new tracking condition

1. Add a member to `RuleType` in `domain/enums.py` **and** an `ALTER TYPE ... ADD VALUE`
   migration.
2. Write an evaluator in `services/rules_engine.py` — a pure function of `RuleContext`
   returning `RuleMatch | None`.
3. Register it against the new `RuleType`.
4. Document any `params` keys it reads (rule parameters live in a JSONB column, so no
   schema change is needed for the settings themselves).

## 14. Running tests

```powershell
pytest                       # unit tests always; integration tests skip without a database
pytest -m "not db"           # unit only, explicitly
ruff check .
mypy
```

Integration tests need `TEST_DATABASE_URL` pointing at a **throwaway** database — the suite
migrates it up and tears it down. Store-extraction tests run against saved fixtures, so the
suite never depends on Amazon or Flipkart being online.

## 15. Docker setup

```powershell
docker compose -f docker/docker-compose.yml up -d db
docker compose -f docker/docker-compose.yml run --rm migrate
docker compose -f docker/docker-compose.yml up api
docker compose -f docker/docker-compose.yml --profile worker up   # phase 5
```

The default image is lean and runs as a non-root user. For browser rendering, build against
the Playwright base image:

```powershell
docker build -f docker/Dockerfile `
  --build-arg BASE_IMAGE=mcr.microsoft.com/playwright/python:v1.47.0-jammy `
  --build-arg EXTRAS=browser -t product-tracker:browser .
```

## 16. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `invalid configuration: ... database_url Field required` | No `DATABASE_URL`. Copy `.env.example` to `.env`. Exit code 4. |
| `DATABASE_URL must be a PostgreSQL DSN` | SQLite/MySQL URL supplied. PostgreSQL is the only supported database. |
| `/health/ready` returns 503, `no alembic_version` | Database reachable but unmigrated. Run `alembic upgrade head`. |
| `status` says "no stores registered" | Run `product-tracker stores sync`. |
| Integration tests all skip | `TEST_DATABASE_URL` is unset. That is intentional, not a failure. |
| Readiness hangs | Should not happen: `DB_CONNECT_TIMEOUT_SECONDS` (default 5) bounds connection attempts. |
| `401` from the API | `API_KEY` is set; send `X-API-Key`. The 401's `WWW-Authenticate` header names it. |
| `413` from the API | Body over `API_MAX_REQUEST_BYTES` (default 64 KB). |
| `status` says the worker is not running | It infers this from overdue jobs in the job store. Start it: `product-tracker worker`. |
| The worker never checks a product added while it was running | Reconcile runs every `RECONCILE_INTERVAL_SECONDS` (default 60). Wait one interval, or restart the worker. |
| A check is recorded as `skipped` | The store's circuit breaker is open after repeated failures. It half-opens after `STORE_CIRCUIT_RESET_SECONDS`. `error_detail` says how long is left. |
| Every job runs twice | Two workers are running against one database. Only run one. |
| An alert fired once and then went quiet | Deduplication. The same alert is delivered once per day; a further price move alerts again. Check `product-tracker alerts history <ID>`. |
| No alert at all | Is the rule enabled, is it within its cooldown, and is a provider configured? `product-tracker alerts list` and `product-tracker config` show all three. |
| Notification status is `suppressed` | No configured provider accepted it. Set `NOTIFY_DEFAULT_PROVIDERS` and the channel's own settings. |
| History has fewer rows than checks | Expected. Only changed prices are appended; every check is in `check_executions`. |
| Stats say `mixed_currency` | The listing has been priced in more than one currency. Statistics cover the most recent one; averaging across currencies would be meaningless. |
| A store reports `blocked` | The site served an anti-bot challenge. This is recorded, not worked around. Try a direct product URL, or accept that the store is unreadable. |
| `Playwright is not installed` | `pip install -e ".[browser]"` then `playwright install chromium`, or set `PLAYWRIGHT_ENABLED=false`. |
| `Docker Desktop is unable to start` (Windows) | Windows Home can only use the WSL2 backend. In an **Administrator** shell: `wsl --install`, then reboot. |
| Docker daemon down after a reboot (Windows) | Docker Desktop does not always auto-start. Run `docker desktop start`, or enable "Start Docker Desktop when you sign in" in its settings. |
| `docker` not found in an already-open shell | The installer edits PATH; existing shells keep the old one. Open a new terminal, or call `…\Programs\DockerDesktop\resources\bin\docker.exe` directly. |

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation: config, database, models, migrations, logging, CLI, API skeleton | **done** |
| 2 | Store adapter interface, generic + Flipkart adapters, URL validation, manual check | **done** |
| 3 | Price/availability history, statistics, change detection | **done** |
| 4 | Tracking rules, notification abstraction, providers, deduplication | **done** |
| 5 | Scheduler, background worker, retries, rate limiting | **done** |
| 6 | Complete API and CLI surface, auth, pagination | **done** |
| 7 | Test coverage, Docker polish, docs, security and performance review | next |
