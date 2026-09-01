"""Seed the retailers verified against live sites.

These all use the generic adapter -- they publish enough structured data for it -- but they
are distinct retailers and should be named as such. Before this, every one of them appeared
as "generic", which made per-store filtering useless and their statistics indistinguishable.

Values are written literally, not imported from the catalogue: a migration is a snapshot of
history, and reading today's catalogue would make a replay years from now produce something
different. Ongoing reconciliation is ``product-tracker stores sync``.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-01
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_STORES: tuple[dict[str, object], ...] = (
    {
        "slug": "vijay-sales",
        "name": "Vijay Sales",
        "domains": ["vijaysales.com"],
        "adapter_key": "generic",
    },
    {
        "slug": "reliance-digital",
        "name": "Reliance Digital",
        "domains": ["reliancedigital.in"],
        "adapter_key": "generic",
    },
    {
        "slug": "bigbasket",
        "name": "BigBasket",
        "domains": ["bigbasket.com"],
        "adapter_key": "generic",
    },
    {
        "slug": "croma",
        "name": "Croma",
        "domains": ["croma.com"],
        "adapter_key": "generic",
    },
)

#: The fallback's display name changed with it: it is no longer the only generic store.
GENERIC_NAME_BEFORE = "Generic (schema.org)"
GENERIC_NAME_AFTER = "Other (schema.org)"


def _quote(value: str) -> str:
    """Render a SQL string literal.

    Values here are fixed constants, never user input, and must be inlined rather than
    bound: ``alembic upgrade --sql`` renders offline with no connection, where bound
    parameters in a ``text()`` construct come out as NULL.
    """
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    for store in SEED_STORES:
        slug = _quote(str(store["slug"]))
        name = _quote(str(store["name"]))
        domains = _quote(json.dumps(store["domains"]))
        adapter_key = _quote(str(store["adapter_key"]))
        op.execute(
            "INSERT INTO stores (slug, name, domains, adapter_key, enabled) "
            f"VALUES ({slug}, {name}, CAST({domains} AS jsonb), {adapter_key}, true) "
            "ON CONFLICT (slug) DO NOTHING"
        )

    op.execute(
        f"UPDATE stores SET name = {_quote(GENERIC_NAME_AFTER)} "
        f"WHERE slug = 'generic' AND name = {_quote(GENERIC_NAME_BEFORE)}"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE stores SET name = {_quote(GENERIC_NAME_BEFORE)} "
        f"WHERE slug = 'generic' AND name = {_quote(GENERIC_NAME_AFTER)}"
    )
    slugs = ", ".join(_quote(str(store["slug"])) for store in SEED_STORES)
    # Only remove stores nothing references, so a downgrade cannot orphan tracked products.
    op.execute(
        f"DELETE FROM stores WHERE slug IN ({slugs}) "
        "AND id NOT IN (SELECT DISTINCT store_id FROM products)"
    )
