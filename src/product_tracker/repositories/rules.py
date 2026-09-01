"""Tracking-rule repository."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from ..db.models import TrackingRule
from ..domain.enums import RuleType
from .base import Repository


class TrackingRuleRepository(Repository[TrackingRule]):
    model = TrackingRule

    def list_for_product(self, product_id: int) -> list[TrackingRule]:
        stmt = (
            select(TrackingRule)
            .where(TrackingRule.product_id == product_id)
            .order_by(TrackingRule.id)
        )
        return list(self.session.execute(stmt).scalars())

    def list_enabled(self, product_id: int) -> list[TrackingRule]:
        """Rules the engine should evaluate for this product.

        Deliberately *not* scoped to a user: one check serves every subscriber, so every
        subscriber's rules must be evaluated against it. Scoping this would mean the first
        user to be checked is the only one who ever gets alerted.
        """
        stmt = (
            select(TrackingRule)
            .where(
                TrackingRule.product_id == product_id,
                TrackingRule.enabled.is_(True),
            )
            .order_by(TrackingRule.id)
        )
        return list(self.session.execute(stmt).scalars())

    def list_page(
        self,
        *,
        limit: int,
        offset: int,
        product_id: int | None = None,
        user_id: int | None = None,
    ) -> list[TrackingRule]:
        stmt = select(TrackingRule)
        if product_id is not None:
            stmt = stmt.where(TrackingRule.product_id == product_id)
        if user_id is not None:
            stmt = stmt.where(TrackingRule.user_id == user_id)
        stmt = stmt.order_by(TrackingRule.id).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars())

    def count_filtered(
        self, *, product_id: int | None = None, user_id: int | None = None
    ) -> int:
        stmt = select(func.count()).select_from(TrackingRule)
        if product_id is not None:
            stmt = stmt.where(TrackingRule.product_id == product_id)
        if user_id is not None:
            stmt = stmt.where(TrackingRule.user_id == user_id)
        return int(self.session.execute(stmt).scalar_one())

    def find(
        self, product_id: int, rule_type: RuleType, user_id: int | None = None
    ) -> TrackingRule | None:
        """An existing rule of this type for this product, if any.

        Scoped by owner where given: two users may each hold a "price below target" on the
        same listing, at different targets, and neither is a duplicate of the other.
        """
        stmt = select(TrackingRule).where(
            TrackingRule.product_id == product_id,
            TrackingRule.rule_type == rule_type,
        )
        if user_id is not None:
            stmt = stmt.where(TrackingRule.user_id == user_id)
        return self.session.execute(stmt).scalars().first()

    def mark_fired(self, rule: TrackingRule, when: datetime) -> None:
        """Stamp the rule so its cooldown starts running."""
        rule.last_fired_at = when
        self.session.flush()
