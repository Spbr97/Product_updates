"""Has a real shop moved a price or a stock state while we were watching?

The acceptance list asks for two things no honest run can force: a price change and an
availability transition, observed live. Nobody can make a retailer move its price on
command, so those two steps sat permanently unverified -- which is the wrong shape for a
gap. Unverifiable-in-principle and not-yet-observed are different things, and only the
second one is true here.

So this is the other way round: track real products, let the worker keep checking them,
and ask this. It reports what the database has actually recorded, from real shops, with
the dates and the amounts. Run it whenever you want to know.

    python scripts/observed.py

Exits 0 once both have been seen at least once, so it can gate a release if you want it
to. A price change usually shows up within days; an availability transition can take
considerably longer, because most things stay in stock.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("LOG_LEVEL", "WARNING")

from sqlalchemy import text

from product_tracker.db.session import session_scope

#: Price observations are only written when the price actually differs from the last one
#: (or the currency does), so a second row for one product IS a real change. That rule is
#: the price-history contract, and it is what makes this query short.
PRICE_CHANGES = """
SELECT p.name, s.name AS shop, h.currency,
       lag(h.price) OVER w AS was, h.price AS now,
       h.observed_at::date AS on_date
FROM price_history h
JOIN products p ON p.id = h.product_id
LEFT JOIN stores s ON s.id = p.store_id
WINDOW w AS (PARTITION BY h.product_id ORDER BY h.observed_at)
ORDER BY h.observed_at DESC
"""

#: Availability history records a row only on a transition, and the contract is explicit
#: that a move out of UNKNOWN is not a became_available. Those are excluded here for the
#: same reason the alert engine excludes them: we did not see it go out of stock, so we
#: cannot claim to have seen it come back.
AVAILABILITY_CHANGES = """
SELECT p.name, s.name AS shop,
       lag(a.availability) OVER w AS was, a.availability AS now,
       a.observed_at::date AS on_date
FROM availability_history a
JOIN products p ON p.id = a.product_id
LEFT JOIN stores s ON s.id = p.store_id
WINDOW w AS (PARTITION BY a.product_id ORDER BY a.observed_at)
ORDER BY a.observed_at DESC
"""

COVERAGE = """
SELECT count(DISTINCT p.id) AS products,
       count(DISTINCT p.store_id) AS shops,
       count(c.id) AS checks,
       min(c.started_at)::date AS watching_since
FROM products p
LEFT JOIN check_executions c ON c.product_id = p.id
WHERE p.tracking_status = 'active'
"""


def main() -> int:
    with session_scope() as session:
        coverage = session.execute(text(COVERAGE)).one()
        prices = [r for r in session.execute(text(PRICE_CHANGES)).all() if r.was is not None]
        moves = [
            r
            for r in session.execute(text(AVAILABILITY_CHANGES)).all()
            if r.was is not None and "unknown" not in (str(r.was), str(r.now))
        ]

    print(
        f"Watching {coverage.products} product(s) across {coverage.shops} shop(s); "
        f"{coverage.checks} checks since {coverage.watching_since or 'never'}.\n"
    )

    print(f"D. Price change      {'OBSERVED' if prices else 'not yet'}")
    for row in prices[:8]:
        print(
            f"     {row.on_date}  {row.shop or '?':<18} {row.currency} {row.was} -> {row.now}"
            f"   {(row.name or '')[:40]}"
        )
    if not prices:
        print("     No product has moved price yet. Leave the worker running.")

    print(f"\nF. Availability      {'OBSERVED' if moves else 'not yet'}")
    for row in moves[:8]:
        print(
            f"     {row.on_date}  {row.shop or '?':<18} {row.was} -> {row.now}"
            f"   {(row.name or '')[:40]}"
        )
    if not moves:
        print(
            "     Nothing has gone out of stock and come back, or the reverse. Transitions\n"
            "     out of 'unknown' do not count and are excluded, for the same reason the\n"
            "     alert engine excludes them: we never saw it leave."
        )

    return 0 if prices and moves else 1


if __name__ == "__main__":
    sys.exit(main())
