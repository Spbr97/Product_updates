"""Name Blinkit, so a blinkit.com URL is attributed rather than lost in "generic".

Blinkit was in the original brief's retailer list and never got a catalogue row. It is a
~10-minute quick-commerce app: no crawlable web catalogue, and every price sits behind a
delivery pincode set in an app session. A check will not succeed -- but a listing tracked
at ``blinkit.com`` should still be shown as Blinkit and reported honestly, the way Croma
and Sangeetha are, not folded into the shared "Other (schema.org)" column.

Same shape as 0008: the row is written with literal constants (a migration is a snapshot;
reading today's catalogue would make a future replay diverge), and any listing already
tracked under ``generic`` for that host is re-filed.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-03
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SLUG = "blinkit"
NAME = "Blinkit"
DOMAIN = "blinkit.com"


def _quote(value: str) -> str:
    """A SQL string literal. Constants only -- inlined so ``upgrade --sql`` renders
    offline without turning bound parameters into NULL."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    slug = _quote(SLUG)
    domains = _quote(json.dumps([DOMAIN]))
    op.execute(
        "INSERT INTO stores (slug, name, domains, adapter_key, enabled) "
        f"VALUES ({slug}, {_quote(NAME)}, CAST({domains} AS jsonb), 'generic', true) "
        "ON CONFLICT (slug) DO NOTHING"
    )

    # Re-file any listing added before Blinkit had a name. Anchored on "://" and the bare
    # domain or a subdomain of it, so "notblinkit.com" cannot match.
    conditions = " OR ".join(
        f"url_canonical LIKE {_quote(p)}"
        for p in (f"%://{DOMAIN}/%", f"%://%.{DOMAIN}/%")
    )
    op.execute(
        f"UPDATE products SET store_id = (SELECT id FROM stores WHERE slug = {slug}) "
        "WHERE store_id = (SELECT id FROM stores WHERE slug = 'generic') "
        f"AND ({conditions})"
    )


def downgrade() -> None:
    slug = _quote(SLUG)
    # Hand listings back before dropping the store, or the RESTRICT on products.store_id
    # blocks the delete.
    op.execute(
        "UPDATE products SET store_id = (SELECT id FROM stores WHERE slug = 'generic') "
        f"WHERE store_id = (SELECT id FROM stores WHERE slug = {slug})"
    )
    op.execute(
        f"DELETE FROM stores WHERE slug = {slug} "
        "AND id NOT IN (SELECT DISTINCT store_id FROM products)"
    )
