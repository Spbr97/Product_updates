"""Let more than one worker run, by claiming a check before performing it.

"One worker per database" was enforced by an advisory lock in ``scheduler/lock.py``: a
second worker refused to start, because APScheduler's job store has no cross-process
locking and two workers would each run every job -- every product checked twice, every
shop hit twice as hard.

Refusing is the right default and stays the default. But it makes the worker a single
point of failure with no failover, and the fix is not a bigger lock: it is to make the
*unit of work* exclusive rather than the process. A worker claims a product's check
atomically before running it, so if two workers fire the same job at the same instant, one
claims it and the other steps aside. The check still happens exactly once.

The claim is a lease, not a lock, because the holder can die: a worker that is killed
mid-check leaves ``check_claimed_at`` set with nothing running. Anything older than the
lease is therefore claimable again. That is the one thing a database-row claim must get
right, and it is why this is two columns and not a boolean.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("check_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "products",
        # Who holds it, for diagnosis: "which worker is stuck on product 42" is the first
        # question anyone asks, and a bare timestamp cannot answer it.
        sa.Column("check_claimed_by", sa.String(length=64), nullable=True),
    )
    # Partial: only claimed rows are ever scanned by the reclaim path, and on a healthy
    # system almost none are claimed at any instant.
    op.create_index(
        "ix_products_check_claimed_at",
        "products",
        ["check_claimed_at"],
        postgresql_where=sa.text("check_claimed_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_products_check_claimed_at", table_name="products")
    op.drop_column("products", "check_claimed_by")
    op.drop_column("products", "check_claimed_at")
