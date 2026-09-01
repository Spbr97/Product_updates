"""Domain enumerations.

These are the vocabulary of the system. They are persisted as native PostgreSQL enum
types (see ``db.models``), so member *values* -- not names -- are the stable contract.
Renaming a value requires a migration; adding a member requires an ``ALTER TYPE``.
"""

from __future__ import annotations

from enum import StrEnum


class Availability(StrEnum):
    """Whether the product can currently be bought.

    Deliberately separate from :class:`FetchOutcome`. A failure to extract a price says
    nothing about stock, so a failed extraction maps to :attr:`UNKNOWN`, never
    :attr:`OUT_OF_STOCK`.
    """

    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNAVAILABLE = "unavailable"  # Listing is gone / delisted / 404.
    UNKNOWN = "unknown"  # We could not determine it. The default.


class TrackingStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class RuleType(StrEnum):
    PRICE_CHANGED = "price_changed"
    PRICE_DROPPED = "price_dropped"
    PRICE_INCREASED = "price_increased"
    PRICE_BELOW_TARGET = "price_below_target"
    BECAME_AVAILABLE = "became_available"
    BECAME_UNAVAILABLE = "became_unavailable"


class CheckStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"  # Fetched something usable, but not everything we wanted.
    SKIPPED = "skipped"  # Throttled, paused, or circuit-broken before any request.


class FetchMethod(StrEnum):
    HTTP = "http"
    BROWSER = "browser"
    NONE = "none"  # No request was made.


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"  # Deduplicated or muted by a cooldown.


class FetchOutcome(StrEnum):
    """Why a fetch ended the way it did.

    Split into transient (worth retrying now) and structural (retrying immediately will
    not help) so the tracking engine can decide without knowing any store's internals.
    """

    OK = "ok"
    PRICE_NOT_FOUND = "price_not_found"
    UNAVAILABLE = "unavailable"
    OUT_OF_STOCK = "out_of_stock"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    BLOCKED = "blocked"  # Anti-bot response. We record it; we never work around it.
    PAGE_STRUCTURE = "page_structure"  # Page loaded but did not look like we expect.
    ERROR = "error"

    @property
    def is_success(self) -> bool:
        """True when the adapter positively determined the product's state.

        ``OUT_OF_STOCK`` and ``UNAVAILABLE`` are successes: we learned the truth, even
        though there is no price to record.
        """
        return self in _SUCCESS_OUTCOMES

    @property
    def is_transient(self) -> bool:
        """True when retrying the same request shortly may succeed."""
        return self in _TRANSIENT_OUTCOMES


_SUCCESS_OUTCOMES = frozenset(
    {FetchOutcome.OK, FetchOutcome.OUT_OF_STOCK, FetchOutcome.UNAVAILABLE}
)
_TRANSIENT_OUTCOMES = frozenset(
    {FetchOutcome.TIMEOUT, FetchOutcome.HTTP_ERROR, FetchOutcome.ERROR}
)


class CellStatus(StrEnum):
    """Why a comparison cell shows what it shows.

    The whole point of this enum is that "no price" has several different meanings, and
    flattening them into one blank cell would repeat -- in the UI this time -- the exact
    mistake the tracker refuses to make in its data model. A shop that blocked us has told
    us nothing about stock; a shop that is genuinely sold out has told us a great deal.
    """

    OK = "ok"  # A price we can stand behind.
    OUT_OF_STOCK = "out_of_stock"  # Listing exists, confirmed not buyable.
    NO_PRICE = "no_price"  # Checked, but no price could be read. Availability unknown.
    BLOCKED = "blocked"  # The store refused the request. Says nothing about the product.
    FAILED = "failed"  # Timeout, HTTP error, or a page we could not parse.
    NEVER_CHECKED = "never_checked"  # Tracked, but no successful check yet.
    NOT_TRACKED = "not_tracked"  # Nobody has added a URL for this variant at this store.


class SearchOutcome(StrEnum):
    """Why a store search returned what it did.

    Separate from :class:`FetchOutcome` for the same reason availability is separate from
    extraction success: "the shop has no such product" and "the shop would not talk to us"
    are completely different answers, and a caller that cannot tell them apart will report
    a product as unavailable at a retailer that simply blocked the search.
    """

    OK = "ok"
    NO_RESULTS = "no_results"  # The search ran and the shop genuinely has nothing.
    BLOCKED = "blocked"  # Refused. Says nothing about whether the shop stocks it.
    PAGE_STRUCTURE = "page_structure"  # Answered, but not in a shape we could read.
    TIMEOUT = "timeout"
    ERROR = "error"
    UNSUPPORTED = "unsupported"  # No search is configured for this store.
    #: The store's results need a browser and none is installed. A deployment fact the
    #: operator can fix, not a statement about the store or the product -- the default
    #: image is deliberately lean, so this will happen and must not read as "no results".
    NEEDS_BROWSER = "needs_browser"
    #: The site's robots.txt asks crawlers not to fetch its search. Not a failure and
    #: not a block we ran into -- a request we chose not to make.
    DISALLOWED = "disallowed"
