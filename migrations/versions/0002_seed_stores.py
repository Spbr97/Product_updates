"""Seed the store catalogue.

Values are written literally rather than imported from
``product_tracker.stores.catalogue``. A migration is a snapshot of history: if it read
today's catalogue, replaying it on a fresh database years from now would produce a
different result. Ongoing reconciliation is ``product-tracker stores sync``.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-01
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_STORES: tuple[dict[str, object], ...] = (
    {
        "slug": "flipkart",
        "name": "Flipkart",
        "domains": ["flipkart.com", "www.flipkart.com", "dl.flipkart.com"],
        "adapter_key": "flipkart",
    },
    {
        "slug": "generic",
        "name": "Generic (schema.org)",
        "domains": [],
        "adapter_key": "generic",
    },
)

def _quote(value: str) -> str:
    """Render a SQL string literal.

    Values here are fixed constants defined above -- never user input -- so inlining them
    is safe. They must be inlined rather than bound: ``alembic upgrade --sql`` renders
    offline with no connection, and bound parameters in a ``text()`` construct come out as
    NULL, which would silently generate broken seed SQL.
    """
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    for store in SEED_STORES:
        slug = _quote(str(store["slug"]))
        name = _quote(str(store["name"]))
        domains = _quote(json.dumps(store["domains"]))
        adapter_key = _quote(str(store["adapter_key"]))
        # Idempotent: a database that already has the row (from `stores sync`) is fine.
        op.execute(
            "INSERT INTO stores (slug, name, domains, adapter_key, enabled) "
            f"VALUES ({slug}, {name}, CAST({domains} AS jsonb), {adapter_key}, true) "
            "ON CONFLICT (slug) DO NOTHING"
        )


def downgrade() -> None:
    slugs = ", ".join(_quote(str(store["slug"])) for store in SEED_STORES)
    # Only remove seeded stores that nothing references, so a downgrade cannot orphan
    # tracked products.
    op.execute(
        f"DELETE FROM stores WHERE slug IN ({slugs}) "
        "AND id NOT IN (SELECT DISTINCT store_id FROM products)"
    )
