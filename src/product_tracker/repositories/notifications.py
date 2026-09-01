"""Notification repository.

The idempotency guarantee lives here. ``create_if_new`` issues
``INSERT ... ON CONFLICT (dedupe_key) DO NOTHING``, so two concurrent workers racing on the
same alert produce exactly one row -- enforced by the database, not by a check-then-insert
that a race could slip between.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert

from ..db.models import Notification
from ..domain.enums import NotificationStatus
from .base import Repository

#: Delivery attempts before a notification is abandoned. Prevents a permanently
#: misconfigured provider from being retried on every check forever.
MAX_DELIVERY_ATTEMPTS = 3


class NotificationRepository(Repository[Notification]):
    model = Notification

    def create_if_new(
        self,
        *,
        product_id: int,
        tracking_rule_id: int | None,
        event_type: str,
        dedupe_key: str,
        payload: dict[str, Any],
    ) -> Notification | None:
        """Insert a notification, or return ``None`` if this alert already exists.

        ``None`` means "already handled" and is the normal, expected outcome of a repeated
        observation -- not an error.
        """
        stmt = (
            insert(Notification)
            .values(
                product_id=product_id,
                tracking_rule_id=tracking_rule_id,
                event_type=event_type,
                dedupe_key=dedupe_key,
                payload=payload,
                status=NotificationStatus.PENDING,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(Notification.id)
        )
        created_id = self.session.execute(stmt).scalar_one_or_none()
        if created_id is None:
            return None
        self.session.flush()
        return self.session.get(Notification, created_id)

    def find_by_dedupe_key(self, dedupe_key: str) -> Notification | None:
        stmt = select(Notification).where(Notification.dedupe_key == dedupe_key)
        return self.session.execute(stmt).scalars().first()

    def list_for_product(
        self, product_id: int, *, limit: int = 20, offset: int = 0
    ) -> list[Notification]:
        stmt = (
            select(Notification)
            .where(Notification.product_id == product_id)
            .order_by(desc(Notification.created_at), desc(Notification.id))
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars())

    def list_retryable(
        self, *, limit: int = 50, product_id: int | None = None
    ) -> list[Notification]:
        """Pending or failed notifications still within their attempt budget."""
        stmt = select(Notification).where(
            Notification.status.in_([NotificationStatus.PENDING, NotificationStatus.FAILED]),
            Notification.attempts < MAX_DELIVERY_ATTEMPTS,
        )
        if product_id is not None:
            stmt = stmt.where(Notification.product_id == product_id)
        stmt = stmt.order_by(Notification.id).limit(limit)
        return list(self.session.execute(stmt).scalars())

    def mark_sent(self, notification: Notification, *, provider: str, when: datetime) -> None:
        notification.status = NotificationStatus.SENT
        notification.provider = provider
        notification.sent_at = when
        notification.error = None
        notification.attempts += 1
        self.session.flush()

    def mark_failed(self, notification: Notification, *, error: str) -> None:
        notification.status = NotificationStatus.FAILED
        notification.error = error[:1000]
        notification.attempts += 1
        self.session.flush()

    def mark_suppressed(self, notification: Notification, *, reason: str) -> None:
        """No provider could take it -- record why rather than retrying forever."""
        notification.status = NotificationStatus.SUPPRESSED
        notification.error = reason[:1000]
        self.session.flush()
