"""Data access layer.

One repository per aggregate. Repositories never commit; the caller owns the transaction.
"""

from .base import Repository
from .products import ProductRepository
from .stores import StoreRepository

__all__ = ["ProductRepository", "Repository", "StoreRepository"]
