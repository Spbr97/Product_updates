"""Pace requests across processes, and remember what kind of thing a group is.

Two unrelated additions that both arrive with concurrent multi-user search.

``store_pacing`` moves throttling out of one process's memory. The in-memory guard was
correct while only the worker made requests; search fans out from CLI processes, so two
people searching at once were two independent rate limiters and five were effectively none.
Probing one retailer too hard is exactly what got a shop to stop answering this machine, so
this is not a theoretical tidy-up.

Postgres rather than Redis, following ``scheduler/lock.py``: the database is already the one
thing every process shares, and it already coordinates the worker's advisory lock.

``product_groups.category`` records what a group holds -- phone, earbuds, powerbank -- so
the right specifications can be read from a title. Nullable: a group that predates this, or
one whose kind cannot be told, is not an error.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "store_pacing"


def upgrade() -> None:
    op.create_table(
        TABLE,
        # The host, not a store slug: several retailers share one adapter, and politeness
        # is owed to a server rather than to the code that happens to read it.
        sa.Column("host", sa.String(255), nullable=False),
        # When the next request to this host may go out. Claimed in advance rather than
        # recorded afterwards -- a process that claims its slot before waiting is one that
        # a second process can see and queue behind.
        sa.Column(
            "next_allowed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        # Set while the circuit is open, so a shop that is refusing everybody is left alone
        # by every process at once rather than by whichever one happened to notice.
        sa.Column("open_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("host", name=f"pk_{TABLE}"),
    )

    op.add_column("product_groups", sa.Column("category", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("product_groups", "category")
    op.drop_table(TABLE)
