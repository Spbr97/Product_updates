"""Send each person's alerts to each person, not all of them to one address.

Alert rules have been per-account since 0007: a rule carries ``user_id``, and one person's
rules are invisible to another. Delivery was not. ``SMTP_TO`` and ``TELEGRAM_CHAT_ID`` are
deployment-wide settings, so every alert every account raised went to the same inbox --
correct for the single-user install this began as, and wrong the moment a second person
has a watchlist.

Two nullable columns, because the global settings must keep working exactly as they do.
A destination here overrides the deployment default for that account; an account with
neither falls back, so nothing changes for an install that never sets one.

Deliberately not a separate table. A destination per channel per account is one row's
worth of information, and a `user_notification_channels` table would buy extensibility
nobody has asked for at the cost of a join on the delivery path.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 320 matches the email column already on this table: the longest address RFC 5321
    # permits. The Telegram id is a numeric chat id or an @channelname, both short, but
    # 64 leaves room without inviting anything odd.
    op.add_column("users", sa.Column("notify_email", sa.String(length=320), nullable=True))
    op.add_column(
        "users", sa.Column("notify_telegram_chat_id", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "notify_telegram_chat_id")
    op.drop_column("users", "notify_email")
