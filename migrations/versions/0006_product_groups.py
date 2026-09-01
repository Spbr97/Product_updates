"""Group listings by the product a person actually means, and by model/colour.

Until now a "product" was one URL at one store, which cannot answer the question people
actually ask: "what does the iPhone 17 256GB Black cost, everywhere?" Two tables supply the
missing identity -- a group ("iPhone 17") and a variant ("256GB / Black") -- and a listing
points at the variant it sells.

``products.variant_id`` is nullable and ``ON DELETE SET NULL``: grouping is an overlay, so
deleting a group must never delete price history, and an ungrouped listing stays perfectly
usable on its own.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("brand", sa.String(100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_product_groups"),
        sa.UniqueConstraint("slug", name="uq_product_groups_slug"),
    )

    op.create_table(
        "product_variants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_product_variants"),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["product_groups.id"],
            name="fk_product_variants_group_id",
            ondelete="CASCADE",
        ),
        # One canonical row per model/colour within a group: this is what stops the same
        # phone being split in two because two shops spell it differently.
        sa.UniqueConstraint("group_id", "label", name="uq_product_variants_group_label"),
    )
    op.create_index("ix_product_variants_group_id", "product_variants", ["group_id"])

    op.add_column("products", sa.Column("variant_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_products_variant_id",
        "products",
        "product_variants",
        ["variant_id"],
        ["id"],
        # SET NULL, not CASCADE: removing a grouping must not destroy price history.
        ondelete="SET NULL",
    )
    # The comparison view fans out from a group to its listings; this is that path.
    op.create_index("ix_products_variant_id", "products", ["variant_id"])


def downgrade() -> None:
    op.drop_index("ix_products_variant_id", table_name="products")
    op.drop_constraint("fk_products_variant_id", "products", type_="foreignkey")
    op.drop_column("products", "variant_id")

    op.drop_index("ix_product_variants_group_id", table_name="product_variants")
    op.drop_table("product_variants")
    op.drop_table("product_groups")
