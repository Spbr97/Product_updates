"""Pure domain types.

Frozen dataclasses with no SQLAlchemy, FastAPI, or HTTP imports. These are the objects
that cross layer boundaries: adapters return them, the tracking engine consumes them,
repositories translate them to and from ORM rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from .enums import (
    Availability,
    CellStatus,
    FetchMethod,
    FetchOutcome,
    RuleType,
    TrackingStatus,
)


@dataclass(frozen=True, slots=True)
class FetchContext:
    """Per-request knobs handed to an adapter by the tracking engine.

    Adapters read settings from here rather than importing global config, which keeps
    them unit-testable without an environment.
    """

    timeout_seconds: int = 25
    allow_browser: bool = True
    user_agent: str = "Mozilla/5.0 (compatible; product-tracker/0.1)"
    accept_language: str = "en-IN,en;q=0.9"
    max_bytes: int = 5_000_000
    #: Re-check that the host is public immediately before connecting, and again after
    #: any redirect. Carried here rather than read from global settings so the policy is
    #: explicit at the call site -- and so disabling it (to track an internal host on
    #: purpose) actually disables it at fetch time, not only at validation time.
    verify_public_host: bool = True


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What an adapter learned about a product in one fetch.

    ``outcome`` and ``availability`` are independent by design. An adapter that cannot
    find a price returns ``outcome=PRICE_NOT_FOUND`` with ``availability=UNKNOWN``; it
    must never report ``OUT_OF_STOCK`` merely because extraction failed.
    """

    outcome: FetchOutcome
    availability: Availability = Availability.UNKNOWN
    name: str | None = None
    product_identifier: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    image_url: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    fetch_method: FetchMethod = FetchMethod.NONE
    http_status: int | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is FetchOutcome.OK and self.price is None:
            raise ValueError("FetchOutcome.OK requires a price; use PRICE_NOT_FOUND instead")
        if self.price is not None and self.currency is None:
            raise ValueError("a price requires a currency")

    @property
    def succeeded(self) -> bool:
        return self.outcome.is_success

    @classmethod
    def failure(
        cls,
        outcome: FetchOutcome,
        message: str,
        *,
        fetch_method: FetchMethod = FetchMethod.NONE,
        http_status: int | None = None,
    ) -> FetchResult:
        """Build a failed result. Availability stays UNKNOWN -- we did not learn it."""
        return cls(
            outcome=outcome,
            availability=Availability.UNKNOWN,
            fetch_method=fetch_method,
            http_status=http_status,
            message=message,
        )


@dataclass(frozen=True, slots=True)
class StoreInfo:
    """A store as advertised by the adapter registry (not the DB row)."""

    slug: str
    display_name: str
    domains: tuple[str, ...]
    adapter_key: str
    is_fallback: bool = False


@dataclass(frozen=True, slots=True)
class ProductSnapshot:
    """A product's state as the rule engine sees it -- detached from any DB session."""

    id: int
    url: str
    store_slug: str
    name: str | None
    current_price: Decimal | None
    currency: str | None
    availability: Availability
    tracking_status: TrackingStatus
    last_checked_at: datetime | None


@dataclass(frozen=True, slots=True)
class PriceStats:
    """Aggregates over a product's recorded price history, in a single currency.

    Two different notions of "change", because people mean both:

    * ``changed_by`` / ``changed_pct`` -- since the *first* observation. "How has this moved
      since I started tracking it."
    * ``changed_from_previous`` / ``changed_pct_from_previous`` -- since the *previous*
      recorded price. "What just happened."

    Reporting only the first was technically right and conversationally wrong.
    """

    currency: str
    observations: int
    current: Decimal | None
    lowest: Decimal | None
    highest: Decimal | None
    average: Decimal | None
    lowest_at: datetime | None
    highest_at: datetime | None
    first_observed_at: datetime | None
    changed_by: Decimal | None = None
    changed_pct: Decimal | None = None
    previous: Decimal | None = None
    previous_observed_at: datetime | None = None
    changed_from_previous: Decimal | None = None
    changed_pct_from_previous: Decimal | None = None
    mixed_currency: bool = False


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Everything a rule evaluator may look at. Evaluators must be pure over this."""

    product: ProductSnapshot
    previous_price: Decimal | None
    current_price: Decimal | None
    previous_availability: Availability
    current_availability: Availability
    stats: PriceStats | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """A fired rule, before it becomes a notification."""

    rule_id: int
    rule_type: RuleType
    product_id: int
    title: str
    body: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    """The provider-agnostic payload handed to a NotificationProvider."""

    title: str
    body: str
    url: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """Whether a check may proceed, and why not if it may not."""

    proceed: bool
    reason: str | None = None

    @classmethod
    def go(cls) -> GuardDecision:
        return cls(proceed=True)

    @classmethod
    def skip(cls, reason: str) -> GuardDecision:
        return cls(proceed=False, reason=reason)


class CheckGuard(Protocol):
    """Paces outgoing requests and can veto a check.

    Implemented by the worker's per-host throttle and circuit breaker. Declared here so
    the tracking engine can consult one without importing the scheduler -- dependencies
    point inward, and the engine is inward of the scheduler.
    """

    def before(self, host: str) -> GuardDecision:
        """Called before a fetch. May block to honour a rate limit."""
        ...

    def after(self, host: str, *, succeeded: bool) -> None:
        """Called after a fetch, with its outcome."""
        ...


@dataclass(frozen=True, slots=True)
class ComparisonCell:
    """One (variant, store) square of the comparison grid."""

    status: CellStatus
    price: Decimal | None = None
    currency: str | None = None
    availability: Availability = Availability.UNKNOWN
    product_id: int | None = None
    url: str | None = None
    last_checked_at: datetime | None = None
    is_stale: bool = False
    #: Movement since the previous recorded price, for an arrow in the UI.
    previous_price: Decimal | None = None

    @property
    def has_price(self) -> bool:
        return self.status is CellStatus.OK and self.price is not None

    @property
    def price_delta(self) -> Decimal | None:
        if self.price is None or self.previous_price is None:
            return None
        return self.price - self.previous_price


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    """One model/colour, priced across every store."""

    variant_id: int | None
    label: str
    attributes: dict[str, str] = field(default_factory=dict)
    cells: dict[str, ComparisonCell] = field(default_factory=dict)

    def _prices(self) -> list[Decimal]:
        """Every price in the row. ``has_price`` already excludes None."""
        return [c.price for c in self.cells.values() if c.has_price and c.price is not None]

    @property
    def best_price(self) -> Decimal | None:
        prices = self._prices()
        return min(prices) if prices else None

    @property
    def best_store_slugs(self) -> tuple[str, ...]:
        """Every store at the best price -- ties are real and should not be hidden."""
        best = self.best_price
        if best is None:
            return ()
        return tuple(
            slug for slug, cell in self.cells.items() if cell.has_price and cell.price == best
        )

    @property
    def spread(self) -> Decimal | None:
        """Cheapest to dearest -- what shopping around is actually worth."""
        prices = self._prices()
        if len(prices) < 2:
            return None
        return max(prices) - min(prices)


@dataclass(frozen=True, slots=True)
class ComparisonMatrix:
    """A whole group priced across models and stores: the user-facing answer."""

    group_slug: str
    group_name: str
    brand: str | None
    store_slugs: tuple[str, ...]
    store_names: dict[str, str]
    rows: tuple[ComparisonRow, ...]
    generated_at: datetime
    #: Set when listings report different currencies. Prices are never converted, so the
    #: "best price" across currencies would be meaningless -- this says so out loud.
    currencies: tuple[str, ...] = ()

    @property
    def mixed_currency(self) -> bool:
        return len(self.currencies) > 1

    @property
    def best_overall(self) -> tuple[ComparisonRow, str] | None:
        """The cheapest (row, store) in the grid. Undefined across currencies."""
        if self.mixed_currency:
            return None
        best: tuple[ComparisonRow, str] | None = None
        for row in self.rows:
            price = row.best_price
            if price is None:
                continue
            if best is None or price < (best[0].best_price or price):
                best = (row, row.best_store_slugs[0])
        return best
