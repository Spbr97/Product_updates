"""Price history repository.

Append-only by contract: this class offers no update or delete. Rows leave only when their
product is deleted and the cascade removes them.

Aggregates are computed **per currency**. A product whose listing switched from USD to INR
has two incomparable series, and averaging across them would produce a number that means
nothing. The current currency -- that of the most recent observation -- wins, and
``PriceStats.mixed_currency`` flags that older rows were excluded.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import desc, func, select

from ..db.models import PriceHistory
from ..domain.models import PriceStats
from .base import Repository


class PriceHistoryRepository(Repository[PriceHistory]):
    model = PriceHistory

    def record(
        self,
        *,
        product_id: int,
        price: Decimal,
        currency: str,
        observed_at: datetime,
        check_execution_id: int | None = None,
    ) -> PriceHistory:
        """Append one observation. Never overwrites an existing row."""
        entry = PriceHistory(
            product_id=product_id,
            price=price,
            currency=currency,
            observed_at=observed_at,
            check_execution_id=check_execution_id,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def latest(self, product_id: int) -> PriceHistory | None:
        """The most recently observed price, whatever its currency."""
        stmt = (
            select(PriceHistory)
            .where(PriceHistory.product_id == product_id)
            .order_by(desc(PriceHistory.observed_at), desc(PriceHistory.id))
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def list_page(self, product_id: int, *, limit: int, offset: int) -> list[PriceHistory]:
        """Newest first -- "what has this cost lately" is the common question."""
        stmt = (
            select(PriceHistory)
            .where(PriceHistory.product_id == product_id)
            .order_by(desc(PriceHistory.observed_at), desc(PriceHistory.id))
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars())

    def count_for_product(self, product_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(PriceHistory)
            .where(PriceHistory.product_id == product_id)
        )
        return int(self.session.execute(stmt).scalar_one())

    def currencies(self, product_id: int) -> list[str]:
        stmt = (
            select(PriceHistory.currency)
            .where(PriceHistory.product_id == product_id)
            .distinct()
            .order_by(PriceHistory.currency)
        )
        return list(self.session.execute(stmt).scalars())

    def stats(self, product_id: int) -> PriceStats | None:
        """Aggregate the product's price history, or ``None`` if it has none."""
        newest = self.latest(product_id)
        if newest is None:
            return None

        currency = newest.currency
        all_currencies = self.currencies(product_id)

        aggregate = self.session.execute(
            select(
                func.count(),
                func.min(PriceHistory.price),
                func.max(PriceHistory.price),
                func.avg(PriceHistory.price),
                func.min(PriceHistory.observed_at),
            ).where(
                PriceHistory.product_id == product_id,
                PriceHistory.currency == currency,
            )
        ).one()
        observations, lowest, highest, average, first_at = aggregate

        first_price = self._price_at_extreme(product_id, currency, oldest=True)
        lowest_at = self._when_price_is(product_id, currency, lowest)
        highest_at = self._when_price_is(product_id, currency, highest)

        current = newest.price
        changed_by = changed_pct = None
        if first_price is not None:
            changed_by = current - first_price
            if first_price != 0:
                # Quantised to two places: a price series does not support more.
                changed_pct = (changed_by / first_price * 100).quantize(Decimal("0.01"))

        return PriceStats(
            currency=currency,
            observations=int(observations),
            current=current,
            lowest=lowest,
            highest=highest,
            # AVG returns more precision than a price has; round to money.
            average=average.quantize(Decimal("0.01")) if average is not None else None,
            lowest_at=lowest_at,
            highest_at=highest_at,
            first_observed_at=first_at,
            changed_by=changed_by,
            changed_pct=changed_pct,
            mixed_currency=len(all_currencies) > 1,
        )

    def _price_at_extreme(
        self, product_id: int, currency: str, *, oldest: bool
    ) -> Decimal | None:
        order = PriceHistory.observed_at.asc() if oldest else PriceHistory.observed_at.desc()
        stmt = (
            select(PriceHistory.price)
            .where(
                PriceHistory.product_id == product_id,
                PriceHistory.currency == currency,
            )
            .order_by(order, PriceHistory.id.asc() if oldest else PriceHistory.id.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def _when_price_is(
        self, product_id: int, currency: str, price: Decimal | None
    ) -> datetime | None:
        """When a given price was *first* seen.

        Earliest rather than latest: "the lowest price occurred on..." should name the
        first time it hit that low, not the most recent repeat of it.
        """
        if price is None:
            return None
        stmt = (
            select(PriceHistory.observed_at)
            .where(
                PriceHistory.product_id == product_id,
                PriceHistory.currency == currency,
                PriceHistory.price == price,
            )
            .order_by(PriceHistory.observed_at.asc(), PriceHistory.id.asc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()
