"""Multiple users, sharing one set of listings.

The important decision is what is *not* per-user. A tracked listing and its price history
stay global, and users subscribe to them:

* two people watching the same Flipkart URL must not mean fetching that page twice --
  politeness towards retailers is a design value here, not an afterthought;
* ``products.url_canonical`` keeps its unique constraint, which is what makes duplicate
  detection work at all;
* a new user inherits the whole existing price history of anything already tracked, rather
  than starting from an empty chart.

What *is* per-user is everything that expresses intent: subscriptions, groups, and alert
rules. Two people can organise the same phone differently and alert on different prices.

Existing rows are backfilled onto a default user, so a single-user install keeps working
with no configuration change.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The account existing data is attributed to. Deliberately has no API key: it is reachable
#: through the legacy single-key configuration and the CLI, not by presenting a credential
#: that was written into a migration.
DEFAULT_USER_EMAIL = "local@localhost"
DEFAULT_USER_NAME = "Local"

#: The default account, referred to by a subselect rather than a fetched id, so the
#: same SQL is valid both online and in an offline ``--sql`` render.
_DEFAULT_USER_ID = f"SELECT id FROM users WHERE email = '{DEFAULT_USER_EMAIL}'"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        # A SHA-256 of the key, never the key. Nothing can read a credential back out.
        sa.Column("api_key_hash", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        # Unique so one key cannot resolve to two accounts, and indexed because every
        # authenticated request looks a user up by exactly this.
        sa.UniqueConstraint("api_key_hash", name="uq_users_api_key_hash"),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        # Pausing is per subscriber. Were it only a column on products, one user pausing
        # would silently stop everyone else's updates for a listing they all watch.
        sa.Column("paused", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_subscriptions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_subscriptions_user_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_subscriptions_product_id", ondelete="CASCADE"
        ),
        # Watching something twice is not a thing; the database enforces that rather than
        # a check-then-insert that a concurrent request could slip between.
        sa.UniqueConstraint("user_id", "product_id", name="uq_subscriptions_user_product"),
    )
    op.create_index("ix_subscriptions_product_id", "subscriptions", ["product_id"])

    _create_default_user()

    _add_owner_column("product_groups")
    _add_owner_column("tracking_rules")

    # A group slug is unique *per user*: two people may each have an "iphone-17".
    op.drop_constraint("uq_product_groups_slug", "product_groups", type_="unique")
    op.create_unique_constraint(
        "uq_product_groups_user_slug", "product_groups", ["user_id", "slug"]
    )

    _make_grouping_per_user()

    # Everything already tracked becomes a subscription of the default user, so nobody
    # loses sight of their products across the upgrade.
    op.execute(
        f"INSERT INTO subscriptions (user_id, product_id, paused) "
        f"SELECT ({_DEFAULT_USER_ID}), id, tracking_status = 'paused' FROM products "
        f"ON CONFLICT DO NOTHING"
    )


def _make_grouping_per_user() -> None:
    """Move the listing-to-model link off the shared product row.

    ``products.variant_id`` was a single column on a row several users share, so it could
    only ever hold one person's grouping. The second user to group a listing silently took
    it out of the first user's comparison -- their grid simply lost a column, with nothing
    to say why. A join table lets each user's grouping coexist, because a variant already
    belongs to a group and a group already belongs to a user.
    """
    op.create_table(
        "variant_listings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("variant_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_variant_listings"),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["product_variants.id"],
            name="fk_variant_listings_variant_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_variant_listings_product_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("variant_id", "product_id", name="uq_variant_listings"),
    )
    op.create_index("ix_variant_listings_product_id", "variant_listings", ["product_id"])

    op.execute(
        "INSERT INTO variant_listings (variant_id, product_id) "
        "SELECT variant_id, id FROM products WHERE variant_id IS NOT NULL"
    )

    op.drop_index("ix_products_variant_id", table_name="products")
    op.drop_constraint("fk_products_variant_id", "products", type_="foreignkey")
    op.drop_column("products", "variant_id")


def _create_default_user() -> None:
    """Insert the account that existing data is attributed to.

    Everything here is plain SQL with inlined literals, and nothing reads a value back.
    ``alembic upgrade --sql`` renders offline against no database at all: bound parameters
    come out as NULL, and ``op.get_bind()`` returns a mock whose ``execute`` yields None,
    so a ``SELECT ... .first()`` to fetch the new id raises AttributeError and the whole
    render dies. Referring to the account by a subselect on its email instead works
    identically online and offline.
    """
    email = DEFAULT_USER_EMAIL.replace("'", "''")
    name = DEFAULT_USER_NAME.replace("'", "''")
    op.execute(
        f"INSERT INTO users (email, name, is_active, is_admin) "
        f"VALUES ('{email}', '{name}', true, true) ON CONFLICT (email) DO NOTHING"
    )


def _add_owner_column(table: str) -> None:
    """Add ``user_id``, backfill it, then make it required.

    Three steps because the column cannot be NOT NULL while existing rows have no owner.
    """
    op.add_column(table, sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute(f"UPDATE {table} SET user_id = ({_DEFAULT_USER_ID}) WHERE user_id IS NULL")
    op.alter_column(table, "user_id", nullable=False)
    op.create_foreign_key(
        f"fk_{table}_user_id", table, "users", ["user_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index(f"ix_{table}_user_id", table, ["user_id"])


def downgrade() -> None:
    # Restoring a globally unique slug can legitimately fail on a database where two users
    # each created an "iphone-17". That is not a flaw in the downgrade: the constraint
    # genuinely cannot hold once multi-user data exists, and failing loudly is correct.
    op.drop_constraint("uq_product_groups_user_slug", "product_groups", type_="unique")
    op.create_unique_constraint("uq_product_groups_slug", "product_groups", ["slug"])

    for table in ("tracking_rules", "product_groups"):
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.drop_constraint(f"fk_{table}_user_id", table, type_="foreignkey")
        op.drop_column(table, "user_id")

    # Restore the single-valued column. Where a listing belongs to several users'
    # groupings, only one survives -- unavoidable, since that is the limitation this
    # change existed to remove.
    op.add_column("products", sa.Column("variant_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_products_variant_id",
        "products",
        "product_variants",
        ["variant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_products_variant_id", "products", ["variant_id"])
    op.execute(
        "UPDATE products SET variant_id = ("
        "SELECT min(variant_id) FROM variant_listings WHERE product_id = products.id)"
    )
    op.drop_index("ix_variant_listings_product_id", table_name="variant_listings")
    op.drop_table("variant_listings")

    op.drop_index("ix_subscriptions_product_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("users")
