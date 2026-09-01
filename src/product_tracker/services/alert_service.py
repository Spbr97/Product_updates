"""Managing tracking rules, and pausing/resuming products.

Rule parameters are validated here, at creation time, so an unusable rule cannot be saved
and then silently never fire.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.models import Product, TrackingRule
from ..domain.enums import RuleType, TrackingStatus
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..notifications.registry import ALL_PROVIDERS
from ..repositories.products import ProductRepository
from ..repositories.rules import TrackingRuleRepository
from .rules_engine import RULE_EVALUATORS, validate_params

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RulePage:
    items: list[TrackingRule]
    total: int
    limit: int
    offset: int


class AlertService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.rules = TrackingRuleRepository(session)
        self.products = ProductRepository(session)

    def add(
        self,
        product_id: int,
        rule_type: RuleType,
        *,
        params: dict[str, Any] | None = None,
        notify_provider: str | None = None,
        cooldown_seconds: int | None = None,
    ) -> TrackingRule:
        """Create a rule. Raises ``DuplicateError`` if this product already has its type."""
        product = self.products.get(product_id)
        if product is None:
            raise NotFoundError("Product", product_id)

        if rule_type not in RULE_EVALUATORS:
            raise ValidationError(f"no evaluator is registered for rule type {rule_type.value}")

        if notify_provider is not None and notify_provider not in ALL_PROVIDERS:
            known = ", ".join(sorted(ALL_PROVIDERS))
            raise ValidationError(
                f"unknown notification provider {notify_provider!r} (known: {known})"
            )

        # One rule of each type per product: two identical "price dropped" rules would
        # only ever produce one notification anyway, because they would deduplicate.
        if self.rules.find(product_id, rule_type) is not None:
            raise DuplicateError("TrackingRule", f"{rule_type.value} on product {product_id}")

        validated = validate_params(rule_type, params or {})

        rule = TrackingRule(
            product_id=product_id,
            rule_type=rule_type,
            params=validated,
            notify_provider=notify_provider,
            cooldown_seconds=cooldown_seconds,
            enabled=True,
        )
        self.rules.add(rule)
        log.info(
            "rule.added",
            rule_id=rule.id,
            product_id=product_id,
            rule_type=rule_type.value,
        )
        return rule

    def get(self, rule_id: int) -> TrackingRule:
        rule = self.rules.get(rule_id)
        if rule is None:
            raise NotFoundError("TrackingRule", rule_id)
        return rule

    def remove(self, rule_id: int) -> None:
        self.rules.delete(self.get(rule_id))
        log.info("rule.removed", rule_id=rule_id)

    def list(
        self, *, product_id: int | None = None, limit: int = 20, offset: int = 0
    ) -> RulePage:
        if product_id is not None:
            if self.products.get(product_id) is None:
                raise NotFoundError("Product", product_id)
            items = self.rules.list_for_product(product_id)
            return RulePage(
                items=items[offset : offset + limit],
                total=len(items),
                limit=limit,
                offset=offset,
            )
        return RulePage(
            items=self.rules.list_all(limit=limit, offset=offset),
            total=self.rules.count(),
            limit=limit,
            offset=offset,
        )

    def set_enabled(self, rule_id: int, enabled: bool) -> TrackingRule:
        rule = self.get(rule_id)
        rule.enabled = enabled
        self.session.flush()
        return rule

    # --- Product tracking status --------------------------------------------------

    def set_tracking_status(self, product_id: int, status: TrackingStatus) -> Product:
        """Pause or resume checks for a product.

        Pausing keeps the product and all of its history; it only stops the scheduler
        picking it up. A manual ``check`` still works, so you can test a paused product.
        """
        product = self.products.get(product_id)
        if product is None:
            raise NotFoundError("Product", product_id)
        product.tracking_status = status
        self.session.flush()
        log.info("product.tracking_status", product_id=product_id, status=status.value)
        return product
