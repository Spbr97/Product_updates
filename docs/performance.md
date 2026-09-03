# Performance

Measured, not estimated. Everything below was produced by probing a running instance
against a seeded dataset; where a number is an extrapolation it says so.

**Method.** 40 Product Entries / 80 products / 4,800 price-history rows / 4,800 check
executions in PostgreSQL 16, counting SQL statements per HTTP request with a SQLAlchemy
`before_cursor_execute` listener. Counting statements rather than timing them is the
point: a query count is a property of the code, while a millisecond figure is a property
of this laptop.

**Caveat, stated plainly.** These runs are one machine — Docker Postgres on Windows, API
in-process via `TestClient`. The *shapes* below (flat vs. growing) are real and portable.
The absolute milliseconds are not a throughput claim, and no load test has been run.

## Query cost per endpoint

| Endpoint | Queries | Wall time | Scales with rows? |
|---|---|---|---|
| `GET /api/v1/products?limit=50` | 4 | ~11 ms | no — flat at 1, 5, 20, 50 |
| `GET /api/v1/product-entries?limit=50` | 6 | ~19 ms | no — flat at 1, 5, 20, 50 |
| `GET /api/v1/product-entries/{id}` | 5 | ~12 ms | n/a |
| `GET /api/v1/products/{id}` | 4 | ~11 ms | n/a |
| `GET /api/v1/products/{id}/history?limit=50` | 6 | ~13 ms | no — paginated |
| `GET /api/v1/products/{id}/stats` | 11 | ~22 ms | no — aggregated in SQL |
| `GET /api/v1/product-entries/{id}/stats` | 20 | ~29 ms | no — bounded by listings per entry |
| `GET /api/v1/alerts?limit=50` | 4 | ~13 ms | no |
| `GET /api/v1/stores` | 0 | ~2 ms | served from the static catalogue |
| `GET /health/ready` | 4 | ~10 ms | n/a |

### The one N+1 this found

`GET /api/v1/product-entries` fetched each listing's last check execution individually:

```
before   1 -> 7    5 -> 15    20 -> 45    50 -> 85 queries    97.8 ms
after    1 -> 6    5 ->  6    20 ->  6    50 ->  6 queries    18.9 ms
```

Fixed with a single `DISTINCT ON (product_id)` — PostgreSQL's idiom for the top row of
each group, which walks `ix_check_executions_product_id_started_at` instead of sorting.

Worth saying why no test caught it: the response body is byte-identical either way. A
suite that asserts *what* an endpoint returns cannot see *what it cost*. The regression
test added alongside asserts the shape — listing two entries must cost no more queries
than listing one — because an exact count breaks on harmless refactors and teaches people
to bump the number rather than look.

## Statistics are aggregated in the database

`min`, `max`, `avg`, `count` and the first/last observation timestamps are computed with
SQL aggregates in `repositories/price_history.py`, not by loading history into Python. A
product with ten years of hourly prices costs the same to summarise as one with ten rows.

## Indexes

Every column used for filtering, ordering, or joining is indexed — 51 indexes across 25
tables. The ones that carry the load:

| Index | Serves |
|---|---|
| `ix_price_history_product_id_observed_at` | history pagination, stats, latest-price lookup |
| `ix_availability_history_product_id_observed_at` | availability transitions |
| `ix_check_executions_product_id_started_at` | the per-listing last check, incl. the `DISTINCT ON` above |
| `ix_products_tracking_status` | the reconcile pass |
| `ix_products_last_checked_at` | staleness in the comparison grid |
| `uq_products_url_canonical` | duplicate detection on add |
| `uq_notifications_dedupe_key` | idempotent alerting — the database enforces single delivery |
| `uq_subscriptions_user_product` | user-scoped listing reads |
| `uq_retailer_listings_active_store` | one live listing per shop per entry (partial, `WHERE deactivated_at IS NULL`) |

## Known scaling boundaries

These are design limits, not defects. Recording where they bite is the point.

**The reconcile pass is O(active products).** `list_schedulable()` loads every active
product each pass to diff against the scheduler's jobs. Measured at 134 µs/product, so
~1.3 s for 10,000 products against a 60 s default interval — about 2% of the window. It
stays comfortable into the low tens of thousands and would need batching or incremental
diffing beyond that. It reads only `products`; no history is touched.

**Connection pool: 5 + 10 overflow per process.** Two processes (API, worker) is at most
30 connections against a default `max_connections` of 100. The worker's APScheduler thread
pool is 4, so it cannot outrun its own pool. Running many API replicas is what would need
`DB_POOL_SIZE` revisited.

**The API rate limiter is per-process and in-memory.** Two API processes each enforce
their own limit. Per-*store* pacing is shared through the `store_pacing` table, so the
politeness guarantee towards retailers holds across processes; the client-facing limit
does not. See `docs/architecture.md`.

**Fetches happen outside the transaction.** A check can take 25 s; holding a transaction
across it would pin a connection and block autovacuum. The fetch completes first, then all
writes land in one short transaction.

## Re-running this

The probes live in the scratchpad rather than the repo — they are throwaway instruments,
not fixtures. What is kept is the regression test
(`TestListingsCostAFixedNumberOfQueries` in `tests/integration/test_api_product_entries.py`),
which is the part that must not rot. To re-measure, seed a database and count statements
with a `before_cursor_execute` listener as described at the top.
