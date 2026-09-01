"""Name three more retailers, and re-file the listings already tracked under "generic".

Samsung's own store, Sangeetha Mobiles, and Amazon India were all reaching the catalogue's
fallback, so a comparison grid rendered them as one shared "Other (schema.org)" column --
three different retailers presented as one shop, and any two of them selling the same
model would have collided in that single column.

The re-filing matters as much as the seeding. Listings added before this migration point
at the fallback store row, and simply adding catalogue entries would leave them there:
the grid would keep showing them as "Other" for as long as they existed, since a product's
store is fixed at the moment it is added.

Values are written literally rather than imported from the catalogue: a migration is a
snapshot of history, and reading today's catalogue would make a replay years from now
produce something different. Ongoing reconciliation is ``product-tracker stores sync``.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-01
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_STORES: tuple[dict[str, object], ...] = (
    {
        "slug": "samsung",
        "name": "Samsung",
        "domains": ["samsung.com"],
        "adapter_key": "generic",
    },
    {
        "slug": "sangeetha",
        "name": "Sangeetha Mobiles",
        "domains": ["sangeethamobiles.com"],
        "adapter_key": "generic",
    },
    {
        "slug": "amazon-in",
        "name": "Amazon India",
        "domains": ["amazon.in"],
        "adapter_key": "generic",
    },
)


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

        # Re-file listings that were added before this store had a name. Matched on the
        # canonical URL's host, anchored to "://" and either the bare domain or a
        # subdomain of it, so a lookalike host such as "notamazon.in" cannot match.
        domain = str(store["domains"][0])  # type: ignore[index]
        host_patterns = (f"%://{domain}/%", f"%://%.{domain}/%")
        conditions = " OR ".join(
            f"url_canonical LIKE {_quote(pattern)}" for pattern in host_patterns
        )
        op.execute(
            f"UPDATE products SET store_id = (SELECT id FROM stores WHERE slug = {slug}) "
            "WHERE store_id = (SELECT id FROM stores WHERE slug = 'generic') "
            f"AND ({conditions})"
        )


def downgrade() -> None:
    generic = "(SELECT id FROM stores WHERE slug = 'generic')"
    for store in SEED_STORES:
        slug = _quote(str(store["slug"]))
        # Hand the listings back before removing the store, or the delete would be blocked
        # by the RESTRICT on products.store_id.
        op.execute(
            f"UPDATE products SET store_id = {generic} "
            f"WHERE store_id = (SELECT id FROM stores WHERE slug = {slug})"
        )

    slugs = ", ".join(_quote(str(store["slug"])) for store in SEED_STORES)
    op.execute(
        f"DELETE FROM stores WHERE slug IN ({slugs}) "
        "AND id NOT IN (SELECT DISTINCT store_id FROM products)"
    )
