"""Reading price and availability history.

A thin orchestration layer over the two history repositories, so the CLI and the API ask
the same questions the same way and neither reaches into SQLAlchemy directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..db.models import AvailabilityHistory, PriceHistory
from ..domain.errors import NotFoundError
from ..domain.models import PriceStats
from ..repositories.availability_history import AvailabilityHistoryRepository
from ..repositories.price_history import PriceHistoryRepository
from ..repositories.products import ProductRepository


@dataclass(frozen=True, slots=True)
class HistoryPage[EntryT]:
    items: list[EntryT]
    total: int
    limit: int
    offset: int


class HistoryService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.products = ProductRepository(session)
        self.prices = PriceHistoryRepository(session)
        self.availabilities = AvailabilityHistoryRepository(session)

    def _require_product(self, product_id: int) -> None:
        """404 for a missing product rather than an empty history for a nonexistent id."""
        if self.products.get(product_id) is None:
            raise NotFoundError("Product", product_id)

    def price_history(
        self, product_id: int, *, limit: int = 20, offset: int = 0
    ) -> HistoryPage[PriceHistory]:
        self._require_product(product_id)
        return HistoryPage(
            items=self.prices.list_page(product_id, limit=limit, offset=offset),
            total=self.prices.count_for_product(product_id),
            limit=limit,
            offset=offset,
        )

    def availability_history(
        self, product_id: int, *, limit: int = 20, offset: int = 0
    ) -> HistoryPage[AvailabilityHistory]:
        self._require_product(product_id)
        return HistoryPage(
            items=self.availabilities.list_page(product_id, limit=limit, offset=offset),
            total=self.availabilities.count_for_product(product_id),
            limit=limit,
            offset=offset,
        )

    def stats(self, product_id: int) -> PriceStats | None:
        """Price statistics, or ``None`` when nothing has been recorded yet."""
        self._require_product(product_id)
        return self.prices.stats(product_id)
