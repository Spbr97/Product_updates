"""Exception hierarchy.

Every exception the application raises on purpose derives from :class:`ProductTrackerError`
so the API and CLI layers can map failures to status codes and exit codes in one place.

Store *fetch* failures are deliberately NOT exceptions in the normal path -- adapters
return a :class:`~product_tracker.domain.models.FetchResult` carrying a
:class:`~product_tracker.domain.enums.FetchOutcome`. These exception types exist for
adapters that prefer to raise internally and for genuinely exceptional conditions.
"""

from __future__ import annotations


class ProductTrackerError(Exception):
    """Base class for all application errors."""


# --- Configuration ---------------------------------------------------------------


class ConfigurationError(ProductTrackerError):
    """Settings are missing or contradictory."""


# --- Input validation ------------------------------------------------------------


class ValidationError(ProductTrackerError):
    """Caller supplied something we will not accept."""


class QuotaExceededError(ValidationError):
    """An account has reached one of its ceilings.

    A ValidationError, so it surfaces as 422 with a message naming the limit rather than as
    a server error. The ceilings exist because one account scheduling unlimited requests can
    get a shared IP blocked by a retailer -- which costs every other user, not just them.
    """

    def __init__(self, what: str, limit: int) -> None:
        super().__init__(
            f"you already have the maximum of {limit} {what}. Remove one first, "
            f"or raise the limit in configuration."
        )
        self.what = what
        self.limit = limit


class InvalidURLError(ValidationError):
    """URL is malformed, uses a disallowed scheme, or embeds credentials."""


class UnsafeURLError(ValidationError):
    """URL resolves to a private/loopback/reserved address (SSRF guard)."""


# --- Persistence -----------------------------------------------------------------


class NotFoundError(ProductTrackerError):
    """A requested record does not exist."""

    def __init__(self, entity: str, identifier: object) -> None:
        super().__init__(f"{entity} {identifier!r} not found")
        self.entity = entity
        self.identifier = identifier


class DuplicateError(ProductTrackerError):
    """A record violating a uniqueness constraint already exists."""

    def __init__(self, entity: str, identifier: object) -> None:
        super().__init__(f"{entity} {identifier!r} already exists")
        self.entity = entity
        self.identifier = identifier


# --- Stores ----------------------------------------------------------------------


class StoreError(ProductTrackerError):
    """Base class for store-adapter failures."""


class NoAdapterError(StoreError):
    """No registered adapter accepted the URL."""


class FetchError(StoreError):
    """A request to a store failed."""


class TransientFetchError(FetchError):
    """A store request failed in a way that is worth retrying."""


class BlockedError(FetchError):
    """The store returned an anti-bot challenge. We record this and stop."""


class PageStructureError(StoreError):
    """The page loaded but did not contain the structure the adapter expects."""


# --- Notifications ---------------------------------------------------------------


class NotificationError(ProductTrackerError):
    """Base class for notification failures."""


class NotificationDeliveryError(NotificationError):
    """A provider could not deliver a message."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.reason = reason


class ProviderNotConfiguredError(NotificationError):
    """A provider was requested but its settings are absent."""
