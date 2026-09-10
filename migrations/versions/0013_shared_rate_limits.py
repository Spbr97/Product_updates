"""Share the API rate limit across processes, the way store pacing already is.

The limiter kept its buckets in a dict, which was honest about itself in its own docstring:
two API processes each enforced their own limit, so the documented ceiling was really the
ceiling *times the number of replicas*. That was fine while one process served everything
and is wrong the moment anyone scales the API, which is exactly when a rate limit matters.

The same argument settled ``store_pacing`` in 0010: the database is the one thing every
process already shares, and this project deliberately does not add Redis for coordination
Postgres can do. This table is that decision applied to the second limiter.

One row per client key, holding a token bucket. Rows are cheap and self-healing -- a key
that stops calling simply stops being refilled -- and a periodic sweep removes the ones
nobody has touched, so a long-lived deployment does not accumulate a row per IP for ever.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_rate_limits",
        # The caller's identity, as `client_key` computes it -- the peer address today.
        sa.Column("client_key", sa.String(length=128), primary_key=True),
        # Fractional on purpose: a bucket refills continuously, and rounding to whole
        # tokens would quietly change the effective rate.
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column(
            "last_refill",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # The sweep deletes by age, so it needs this; the hot path is a primary-key hit.
    op.create_index("ix_api_rate_limits_last_refill", "api_rate_limits", ["last_refill"])


def downgrade() -> None:
    op.drop_index("ix_api_rate_limits_last_refill", table_name="api_rate_limits")
    op.drop_table("api_rate_limits")
