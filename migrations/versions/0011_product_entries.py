"""One logical product per user, owning one listing per retailer.

Until now the only things a user owned were tracked URLs and, above them, product *groups*
-- a comparison layer spanning models and colours. Neither is the thing a person actually
means by "the Galaxy S25 I am watching": one is a URL, the other is a folder.

A Product Entry is that missing identity. It keeps its id while prices move, while a
retailer URL is replaced, and while one shop stops stocking the product, because every
observation belongs to the retailer listing that saw it rather than to the entry. A price
change must never look like a new product.

Three tables, no data migration:

* ``product_entries`` -- the identity. ``canonical_name`` is deliberately not unique: two
  people may track the same phone, and one person may keep two entries named alike over
  different listings. Uniqueness by name would be a guess about intent.
* ``retailer_listings`` -- a thin layer over ``products``. The product row stays the
  tracking target (history, executions and rules all key on it, and it is shared between
  users); this row carries what belongs to *this user's entry*: which entry, the name they
  gave it at that shop, and whether they still want it. ``product_id`` is RESTRICT so
  removing a listing can never take a shared product and its observations with it.
* ``retailer_listing_url_audits`` -- that a URL was replaced, and by what. Kept apart from
  price history because re-pointing a listing must not rewrite a single observation: the
  old prices were genuinely seen at the old URL.

The partial unique index is the one that matters. ``(product_entry_id, store_slug) WHERE
deactivated_at IS NULL`` allows at most one *active* listing per retailer per entry, while
still letting a deactivated Amazon listing sit beside its replacement -- which is exactly
what a URL change leaves behind.

Existing products, groups and variants are untouched. Entries are forward-only.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTRIES = "product_entries"
LISTINGS = "retailer_listings"
AUDITS = "retailer_listing_url_audits"

# ``create_type=False`` keeps the column definition from re-issuing CREATE TYPE; the type
# is created once below and dropped once on the way down. Same contract as 0001.
ENTRY_STATUS = postgresql.ENUM(
    "active", "archived", name="product_entry_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    ENTRY_STATUS.create(bind, checkfirst=True)

    op.create_table(
        ENTRIES,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("canonical_name", sa.String(length=200), nullable=False),
        sa.Column(
            "status", ENTRY_STATUS, nullable=False, server_default=sa.text("'active'")
        ),
        # Archive rather than delete: the listings below hold months of observations.
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_product_entries_user_id", ENTRIES, ["user_id"])

    op.create_table(
        LISTINGS,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "product_entry_id",
            sa.Integer(),
            sa.ForeignKey(f"{ENTRIES}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # RESTRICT: a listing must never be able to delete a shared product row.
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("store_slug", sa.String(length=64), nullable=False),
        # The user's own wording for this shop, not the scraped title.
        sa.Column("product_name", sa.String(length=200), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_retailer_listings_active_store",
        LISTINGS,
        ["product_entry_id", "store_slug"],
        unique=True,
        postgresql_where=sa.text("deactivated_at IS NULL"),
    )
    op.create_index("ix_retailer_listings_entry_id", LISTINGS, ["product_entry_id"])
    op.create_index("ix_retailer_listings_product_id", LISTINGS, ["product_id"])

    op.create_table(
        AUDITS,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "retailer_listing_id",
            sa.Integer(),
            sa.ForeignKey(f"{LISTINGS}.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("old_url", sa.Text(), nullable=False),
        sa.Column("new_url", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_listing_url_audits_listing_id", AUDITS, ["retailer_listing_id"])


def downgrade() -> None:
    # Tables first, then the type they depend on. Dropping the enum while a column still
    # uses it fails, and leaving it behind makes the second `upgrade` in CI's
    # up/down/up run fall over on CREATE TYPE -- which is the whole point of that job.
    op.drop_index("ix_listing_url_audits_listing_id", table_name=AUDITS)
    op.drop_table(AUDITS)

    op.drop_index("ix_retailer_listings_product_id", table_name=LISTINGS)
    op.drop_index("ix_retailer_listings_entry_id", table_name=LISTINGS)
    op.drop_index("uq_retailer_listings_active_store", table_name=LISTINGS)
    op.drop_table(LISTINGS)

    op.drop_index("ix_product_entries_user_id", table_name=ENTRIES)
    op.drop_table(ENTRIES)

    ENTRY_STATUS.drop(op.get_bind(), checkfirst=True)
