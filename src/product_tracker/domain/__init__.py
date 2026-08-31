"""Domain layer: enums, value objects, and the exception hierarchy.

This package has no dependencies on the database, the web framework, or any store.
Everything else may import it; it imports nothing from the rest of the application.
"""

from .enums import (
    Availability,
    CheckStatus,
    FetchMethod,
    FetchOutcome,
    NotificationStatus,
    RuleType,
    TrackingStatus,
)
from .errors import (
    BlockedError,
    ConfigurationError,
    DuplicateError,
    FetchError,
    InvalidURLError,
    NoAdapterError,
    NotFoundError,
    NotificationDeliveryError,
    NotificationError,
    PageStructureError,
    ProductTrackerError,
    ProviderNotConfiguredError,
    StoreError,
    TransientFetchError,
    UnsafeURLError,
    ValidationError,
)
from .models import (
    FetchContext,
    FetchResult,
    NotificationMessage,
    PriceStats,
    ProductSnapshot,
    RuleContext,
    RuleMatch,
    StoreInfo,
)

__all__ = [
    "Availability",
    "BlockedError",
    "CheckStatus",
    "ConfigurationError",
    "DuplicateError",
    "FetchContext",
    "FetchError",
    "FetchMethod",
    "FetchOutcome",
    "FetchResult",
    "InvalidURLError",
    "NoAdapterError",
    "NotFoundError",
    "NotificationDeliveryError",
    "NotificationError",
    "NotificationMessage",
    "NotificationStatus",
    "PageStructureError",
    "PriceStats",
    "ProductSnapshot",
    "ProductTrackerError",
    "ProviderNotConfiguredError",
    "RuleContext",
    "RuleMatch",
    "RuleType",
    "StoreError",
    "StoreInfo",
    "TrackingStatus",
    "TransientFetchError",
    "UnsafeURLError",
    "ValidationError",
]
