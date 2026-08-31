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
