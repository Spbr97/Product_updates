"""Applying a declarative tracking file.

The original brief configured the whole monitor from a ``config.yaml``: the product, the
retailers, the schedule, the terms to exclude. The rebuild dissolved that -- deployment
settings went to the environment, retailers to the store catalogue, matching to the search
scorer -- and nothing took over the one job that file did which none of those do: saying
*what this installation is watching*, in a form a person can read, edit and commit.

This is that, and only that. It creates products, groups and alert rules from a file; it
owns no settings. A setting expressed in two places is a setting that drifts, and ``.env``
already won that argument.

**Applying is idempotent.** A listing the user already tracks is reported as
``already_tracked`` rather than raising, so the file can be re-applied after a rebuild, or
kept in version control and applied on a new machine, without anyone having to reason
about what is already there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.logging import get_logger
from ..domain.enums import RuleType
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..stores.registry import StoreRegistry
from ..utils.urls import canonicalize_url
from . import group_service
from .alert_service import AlertService
from .product_service import ProductService

log = get_logger(__name__)

#: Rule types that are meaningless without a target price.
_NEEDS_TARGET = {RuleType.PRICE_BELOW_TARGET}


@dataclass
class ListingOutcome:
    url: str
    status: str  # tracked | already_tracked | failed
    product_id: int | None = None
    detail: str | None = None


@dataclass
class ProductOutcome:
    name: str
    group_slug: str | None = None
    listings: list[ListingOutcome] = field(default_factory=list)
    alerts_added: int = 0


@dataclass
class Plan:
    """What a file asks for, after parsing but before anything is written."""

    delivery_pincode: str | None
    products: list[dict[str, Any]]


@dataclass
class Report:
    products: list[ProductOutcome] = field(default_factory=list)
    #: Set when the file declares a delivery area the environment does not have.
    pincode_advice: str | None = None

    def _count(self, status: str) -> int:
        return sum(
            1
            for product in self.products
            for listing in product.listings
            if listing.status == status
        )

    @property
    def tracked(self) -> int:
        return self._count("tracked")

    @property
    def already(self) -> int:
        return self._count("already_tracked")

    @property
    def failed(self) -> int:
        return self._count("failed")


def load(path: Path) -> Plan:
    """Read and validate the file's shape.

    Every failure names the field it came from. A config file that reports "invalid" and
    leaves you to find which of forty lines it meant is worse than no validation at all.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValidationError(f"{path} should be a mapping, not {type(raw).__name__}")

    products = raw.get("products") or []
    if not isinstance(products, list):
        raise ValidationError("'products' should be a list")

    for index, entry in enumerate(products):
        where = f"products[{index}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{where} should be a mapping")
        if not str(entry.get("name", "")).strip():
            raise ValidationError(f"{where} needs a name")
        listings = entry.get("listings") or []
        if not isinstance(listings, list) or not listings:
            raise ValidationError(f"{where} needs at least one URL under 'listings'")
        for alert in entry.get("alerts") or []:
            if not isinstance(alert, dict) or "type" not in alert:
                raise ValidationError(f"{where}: every alert needs a 'type'")
            try:
                rule = RuleType(str(alert["type"]))
            except ValueError as exc:
                allowed = ", ".join(r.value for r in RuleType)
                raise ValidationError(
                    f"{where}: unknown alert type {alert['type']!r}. One of: {allowed}"
                ) from exc
            if rule in _NEEDS_TARGET and alert.get("target") is None:
                raise ValidationError(f"{where}: {rule.value} needs a 'target'")

    pincode = raw.get("delivery_pincode")
    return Plan(
        delivery_pincode=str(pincode) if pincode not in (None, "") else None,
        products=products,
    )


def apply(
    session: Session,
    plan: Plan,
    *,
    user_id: int,
    settings: Settings,
    registry: StoreRegistry,
) -> Report:
    """Create everything the plan asks for that does not exist yet."""
    report = Report(pincode_advice=_pincode_advice(plan, settings))
    products = ProductService(session, registry, settings, user_id)
    alerts = AlertService(session, user_id)

    for entry in plan.products:
        outcome = ProductOutcome(name=str(entry["name"]).strip())
        outcome.group_slug = _ensure_group(
            session, entry, user_id=user_id, settings=settings
        )
        for url in entry["listings"]:
            outcome.listings.append(
                _track_one(
                    products,
                    alerts,
                    session,
                    url=str(url).strip(),
                    entry=entry,
                    group_slug=outcome.group_slug,
                    user_id=user_id,
                    outcome=outcome,
                )
            )
        report.products.append(outcome)

    session.flush()
    log.info(
        "provisioning.applied",
        tracked=report.tracked,
        already_tracked=report.already,
        failed=report.failed,
    )
    return report


def _pincode_advice(plan: Plan, settings: Settings) -> str | None:
    """Never written for the user: settings come from the environment, deliberately.

    Silently adopting a delivery area from this file would give one setting two sources,
    and the one that lost would be invisible. Saying so is the whole contribution.
    """
    wanted = plan.delivery_pincode
    if wanted is None or settings.delivery_pincode == wanted:
        return None
    if settings.delivery_pincode is None:
        return (
            f"this file asks for delivery area {wanted}, but DELIVERY_PINCODE is not "
            f"set, so every check takes whatever each shop's default area returns. "
            f"Add DELIVERY_PINCODE={wanted} to .env"
        )
    return (
        f"this file asks for delivery area {wanted}, but DELIVERY_PINCODE is "
        f"{settings.delivery_pincode}. The environment wins; change one of them."
    )


def _ensure_group(
    session: Session, entry: dict[str, Any], *, user_id: int, settings: Settings
) -> str | None:
    """The comparison group, created only if it is not already there."""
    raw = entry.get("group")
    if not raw:
        return None
    slug = str(raw).strip()
    try:
        group_service.get_group(session, user_id, slug)
    except NotFoundError:
        group_service.create_group(
            session,
            user_id=user_id,
            slug=slug,
            name=str(entry["name"]).strip(),
            brand=(str(entry["brand"]).strip() if entry.get("brand") else None),
            settings=settings,
        )
        session.flush()
    return slug


def _track_one(
    products: ProductService,
    alerts: AlertService,
    session: Session,
    *,
    url: str,
    entry: dict[str, Any],
    group_slug: str | None,
    user_id: int,
    outcome: ProductOutcome,
) -> ListingOutcome:
    """Add one listing, group it, and give it the product's alert rules.

    One bad URL reports itself and the rest of the file still applies. A provisioning run
    that abandons everything after the first typo is a worse tool than one that tells you
    which line to fix.
    """
    try:
        product = products.add(url)
        session.flush()
        status = "tracked"
    except DuplicateError:
        # Look it up the way the duplicate was detected: by canonical URL, since the
        # written form may carry tracking parameters the stored one does not.
        existing = products.products.get_by_canonical_url(canonicalize_url(url))
        if existing is None:
            return ListingOutcome(url=url, status="failed", detail="already tracked")
        product, status = existing, "already_tracked"
    except Exception as exc:
        return ListingOutcome(url=url, status="failed", detail=str(exc))

    if group_slug:
        try:
            group_service.attach_product(
                session,
                product.id,
                user_id=user_id,
                group_slug=group_slug,
                label=(str(entry["label"]).strip() if entry.get("label") else None),
            )
            session.flush()
        except Exception as exc:
            # A listing whose model cannot be inferred is still tracked; it just is not
            # in the grid. Refusing the whole listing over that would be a poor trade.
            log.info("provisioning.group_skipped", url=url, reason=str(exc))

    for alert in entry.get("alerts") or []:
        params = {"target_price": str(alert["target"])} if alert.get("target") else None
        try:
            alerts.add(product.id, RuleType(str(alert["type"])), params=params)
            session.flush()
            outcome.alerts_added += 1
        except DuplicateError:
            pass  # this rule is already on this listing

    return ListingOutcome(url=url, status=status, product_id=product.id)
