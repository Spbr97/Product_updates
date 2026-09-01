"""Tracking-rule repository."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

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
        """Rules the engine should evaluate for this product."""
        stmt = (
            select(TrackingRule)
            .where(
                TrackingRule.product_id == product_id,
                TrackingRule.enabled.is_(True),
            )
            .order_by(TrackingRule.id)
        )
        return list(self.session.execute(stmt).scalars())

    def list_all(self, *, limit: int, offset: int) -> list[TrackingRule]:
        stmt = select(TrackingRule).order_by(TrackingRule.id).limit(limit).offset(offset)
        return list(self.session.execute(stmt).scalars())

    def find(self, product_id: int, rule_type: RuleType) -> TrackingRule | None:
        """An existing rule of this type for this product, if any."""
        stmt = select(TrackingRule).where(
            TrackingRule.product_id == product_id,
            TrackingRule.rule_type == rule_type,
        )
        return self.session.execute(stmt).scalars().first()

    def mark_fired(self, rule: TrackingRule, when: datetime) -> None:
        """Stamp the rule so its cooldown starts running."""
        rule.last_fired_at = when
        self.session.flush()
