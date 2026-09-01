"""Point the Amazon store row at its own adapter.

Migration 0008 seeded Amazon India with ``adapter_key = 'generic'``, which was true at the
time: the generic schema.org reader was all there was. Amazon pages carry no JSON-LD, so
that reader could not find a price, and a page-wide price selector would have returned a
recommendation tile's price instead of the product's.

``stores.adapter_key`` does not choose the adapter at runtime -- the registry matches on
domain -- but it is what ``stores`` and the API report, so leaving it stale would have the
tool describe itself incorrectly.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE stores SET adapter_key = 'amazon-in' WHERE slug = 'amazon-in'")


def downgrade() -> None:
    op.execute("UPDATE stores SET adapter_key = 'generic' WHERE slug = 'amazon-in'")
