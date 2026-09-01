"""Data access layer.

One repository per aggregate. Repositories never commit; the caller owns the transaction.
"""

from .availability_history import AvailabilityHistoryRepository
from .base import Repository
from .executions import CheckExecutionRepository
from .price_history import PriceHistoryRepository
from .products import ProductRepository
from .stores import StoreRepository

__all__ = [
    "AvailabilityHistoryRepository",
    "CheckExecutionRepository",
    "PriceHistoryRepository",
    "ProductRepository",
    "Repository",
    "StoreRepository",
]
