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
from ..repositories.users import SubscriptionRepository
from .rules_engine import RULE_EVALUATORS, validate_params

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RulePage:
    items: list[TrackingRule]
    total: int
    limit: int
    offset: int


class AlertService:
    """Alert rules for one account.

    Every method is scoped to ``user_id``. A rule belonging to somebody else is reported as
    not found rather than forbidden, so a caller cannot enumerate other people's alerts by
    watching which ids come back 403 instead of 404.
    """

    def __init__(self, session: Session, user_id: int) -> None:
        self.session = session
        self.user_id = user_id
        self.rules = TrackingRuleRepository(session)
        self.products = ProductRepository(session)
        self.subscriptions = SubscriptionRepository(session)

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

        # One rule of each type per product *per user*: two identical "price dropped"
        # rules would only ever produce one notification anyway, because they would
        # deduplicate. Another user's rule on the same listing is not a duplicate of mine.
        if self.rules.find(product_id, rule_type, user_id=self.user_id) is not None:
            raise DuplicateError("TrackingRule", f"{rule_type.value} on product {product_id}")

        validated = validate_params(rule_type, params or {})

        rule = TrackingRule(
            product_id=product_id,
            user_id=self.user_id,
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
        if rule is None or rule.user_id != self.user_id:
            raise NotFoundError("TrackingRule", rule_id)
        return rule

    def remove(self, rule_id: int) -> None:
        self.rules.delete(self.get(rule_id))
        log.info("rule.removed", rule_id=rule_id)

    def list(
        self, *, product_id: int | None = None, limit: int = 20, offset: int = 0
    ) -> RulePage:
        if product_id is not None and self.products.get(product_id) is None:
            raise NotFoundError("Product", product_id)
        return RulePage(
            items=self.rules.list_page(
                limit=limit, offset=offset, product_id=product_id, user_id=self.user_id
            ),
            total=self.rules.count_filtered(product_id=product_id, user_id=self.user_id),
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
        """Pause or resume checks for a product, for *this* user.

        Pausing keeps the product and all of its history; it only stops the scheduler
        picking it up. A manual ``check`` still works, so you can test a paused product.

        The pause is recorded on this user's subscription, not on the shared listing. A
        listing several people watch would otherwise go quiet for all of them the moment
        one person paused it -- with nothing in their own view to explain why. The listing
        itself stays active while *anyone* still wants it, which is what
        :meth:`_recompute_tracking_status` works out.
        """
        product = self.products.get(product_id)
        if product is None:
            raise NotFoundError("Product", product_id)

        subscription = self.subscriptions.for_user_and_product(self.user_id, product_id)
        if subscription is None:
            raise NotFoundError("Product", product_id)
        subscription.paused = status is TrackingStatus.PAUSED
        self.session.flush()

        self._recompute_tracking_status(product)
        log.info(
            "product.tracking_status",
            product_id=product_id,
            user_id=self.user_id,
            requested=status.value,
            effective=product.tracking_status.value,
        )
        return product

    def _recompute_tracking_status(self, product: Product) -> None:
        """A listing is checked while at least one subscriber wants it checked."""
        wanted = self.subscriptions.has_active_subscriber(product.id)
        product.tracking_status = TrackingStatus.ACTIVE if wanted else TrackingStatus.PAUSED
        self.session.flush()
