"""Turning a listing's state into something a page can say without lying.

The whole reason this module exists is one failure mode. A retailer that refused our
request, a page whose price we could not parse, and a product that is genuinely sold out
are three different facts with three different responses from the user -- and a UI that
renders all of them as "Out of stock" turns a tracker into a rumour mill.

So every listing resolves to exactly one of the eight states below, each with its own
wording and its own tone, and "we could not tell" is one of them rather than a gap to be
filled with the nearest confident-sounding alternative.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..db.models import RetailerListing
from ..domain.enums import Availability, CheckStatus, TrackingStatus


class ListingState(StrEnum):
    """What a page may say about one retailer. Exhaustive on purpose."""

    NOT_CHECKED = "not_checked"
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"
    PAUSED = "paused"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class StateView:
    """One state, ready to render: a label, a tone, and an honest explanation."""

    state: ListingState
    label: str
    tone: str
    explanation: str


#: The tone names are CSS classes, not colours, so the stylesheet decides what "caution"
#: looks like in light and dark without this module knowing.
_VIEWS: dict[ListingState, StateView] = {
    ListingState.NOT_CHECKED: StateView(
        ListingState.NOT_CHECKED,
        "Not checked yet",
        "muted",
        "Added, but no check has run. The first price arrives with the next scheduled pass.",
    ),
    ListingState.IN_STOCK: StateView(
        ListingState.IN_STOCK,
        "In stock",
        "good",
        "The shop says this is available.",
    ),
    ListingState.OUT_OF_STOCK: StateView(
        ListingState.OUT_OF_STOCK,
        "Out of stock",
        "bad",
        "The shop says this is unavailable. This is the shop's own statement, not a guess.",
    ),
    ListingState.UNKNOWN: StateView(
        ListingState.UNKNOWN,
        "Stock unknown",
        "caution",
        "The page loaded but published nothing reliable about availability. "
        "Unknown is the honest answer; it does not mean out of stock.",
    ),
    ListingState.BLOCKED: StateView(
        ListingState.BLOCKED,
        "Shop refused us",
        "caution",
        "The retailer declined the request. This says nothing about the price or the "
        "stock -- only that we were not allowed to look.",
    ),
    ListingState.FAILED: StateView(
        ListingState.FAILED,
        "Check failed",
        "bad",
        "The last check did not complete. The product may be perfectly fine; we could "
        "not read it.",
    ),
    ListingState.SKIPPED: StateView(
        ListingState.SKIPPED,
        "Check skipped",
        "muted",
        "Nothing was attempted: this shop is being backed off after repeated failures.",
    ),
    ListingState.PAUSED: StateView(
        ListingState.PAUSED,
        "Paused",
        "muted",
        "Scheduled checks are stopped for this product. History is kept.",
    ),
    ListingState.REMOVED: StateView(
        ListingState.REMOVED,
        "Removed",
        "muted",
        "You stopped tracking this shop. Everything already recorded is still here.",
    ),
}

#: Error types that mean "the shop would not talk to us", as opposed to "we read it and
#: could not find a price". Kept apart because only the second says anything about stock.
_BLOCKED_ERRORS = frozenset({"blocked", "unavailable"})


def describe(
    listing: RetailerListing,
    *,
    last_status: CheckStatus | None = None,
    last_error: str | None = None,
) -> StateView:
    """The one state this listing is in.

    Order matters. A removed or paused listing is described that way whatever its last
    check said, because the user's own action is the more relevant fact. After that a
    successful read wins, then the reason a read failed -- and only a shop that actually
    said "unavailable" produces "Out of stock".
    """
    if not listing.is_active:
        return _VIEWS[ListingState.REMOVED]

    product = listing.product
    if product.tracking_status is TrackingStatus.PAUSED:
        return _VIEWS[ListingState.PAUSED]

    if product.last_checked_at is None and last_status is None:
        return _VIEWS[ListingState.NOT_CHECKED]

    if last_status is CheckStatus.SKIPPED:
        return _VIEWS[ListingState.SKIPPED]

    # A shop's own statement about stock outranks how the check was classified: a listing
    # can be read perfectly and still be sold out.
    if product.availability is Availability.OUT_OF_STOCK:
        return _VIEWS[ListingState.OUT_OF_STOCK]
    if product.availability is Availability.IN_STOCK:
        return _VIEWS[ListingState.IN_STOCK]

    if last_error in _BLOCKED_ERRORS:
        return _VIEWS[ListingState.BLOCKED]
    if last_status is CheckStatus.FAILED:
        return _VIEWS[ListingState.FAILED]

    # Read, but it told us nothing definite. Not a failure, and emphatically not a
    # statement that the product is unavailable.
    return _VIEWS[ListingState.UNKNOWN]


def price_text(listing: RetailerListing) -> str:
    """The price, or a dash. Never a zero, and never a guess."""
    product = listing.product
    if product.current_price is None:
        return "—"
    symbol = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}.get(
        product.currency or "", ""
    )
    return f"{symbol}{product.current_price:,.2f}".rstrip("0").rstrip(".")


def cheapest(listings: list[RetailerListing]) -> int | None:
    """The id of the listing with the lowest price, when that is a meaningful question.

    ``None`` when fewer than two shops have a price, or when they are priced in different
    currencies -- comparing those without a conversion policy would produce a "best deal"
    that is simply wrong.
    """
    priced = [
        (item.product.current_price, item)
        for item in listings
        if item.is_active and item.product.current_price is not None
    ]
    if len(priced) < 2:
        return None
    if len({item.product.currency for _, item in priced}) > 1:
        return None
    _, best = min(priced, key=lambda pair: pair[0])
    return int(best.id)
