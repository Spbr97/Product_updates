"""Create APScheduler's job table.

APScheduler would create this itself on first use, but then it would sit outside Alembic:
``downgrade base`` would leave it behind, and nobody reading the migrations would know the
table exists. Creating it here keeps the schema fully described in one place;
APScheduler's own create-if-missing then finds it and does nothing.

The column definitions must match ``apscheduler.jobstores.sqlalchemy`` exactly.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "apscheduler_jobs"


def upgrade() -> None:
    op.create_table(
        TABLE,
        # 191 characters: APScheduler's own limit, chosen for MySQL index compatibility.
        sa.Column("id", sa.Unicode(191), nullable=False),
        sa.Column("next_run_time", sa.Float(25), nullable=True),
        sa.Column("job_state", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{TABLE}"),
    )
    # The scheduler's hot path: "which jobs are due?"
    op.create_index(f"ix_{TABLE}_next_run_time", TABLE, ["next_run_time"])


def downgrade() -> None:
    op.drop_index(f"ix_{TABLE}_next_run_time", table_name=TABLE)
    op.drop_table(TABLE)
