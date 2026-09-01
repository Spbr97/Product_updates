"""Record worker liveness directly instead of inferring it.

Liveness was inferred from overdue jobs in the scheduler's store. That cannot distinguish
"no worker is running" from "a worker is running but wedged mid-check", and it produces a
false alarm whenever a check legitimately runs long. A row the worker touches on every
reconcile answers the question directly.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "worker_heartbeats"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{TABLE}"),
        # One row per worker process: a restart replaces its own row, and a second worker
        # shows up as a second row rather than overwriting the first.
        sa.UniqueConstraint("worker_id", name=f"uq_{TABLE}_worker_id"),
    )
    op.create_index(f"ix_{TABLE}_last_seen_at", TABLE, ["last_seen_at"])


def downgrade() -> None:
    op.drop_index(f"ix_{TABLE}_last_seen_at", table_name=TABLE)
    op.drop_table(TABLE)
