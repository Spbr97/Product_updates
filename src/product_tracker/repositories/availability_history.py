"""Availability history repository.

Append-only, and written only on *transitions*: one row per change, not one per check.
Recording every check would make the table grow without adding information, and
"when did this go out of stock?" is exactly a question about transitions.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, func, select

from ..db.models import AvailabilityHistory
from ..domain.enums import Availability
from .base import Repository


class AvailabilityHistoryRepository(Repository[AvailabilityHistory]):
    model = AvailabilityHistory

    def record(
        self,
        *,
        product_id: int,
        availability: Availability,
        observed_at: datetime,
        check_execution_id: int | None = None,
    ) -> AvailabilityHistory:
        entry = AvailabilityHistory(
            product_id=product_id,
            availability=availability,
            observed_at=observed_at,
            check_execution_id=check_execution_id,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def latest(self, product_id: int) -> AvailabilityHistory | None:
        stmt = (
            select(AvailabilityHistory)
            .where(AvailabilityHistory.product_id == product_id)
            .order_by(desc(AvailabilityHistory.observed_at), desc(AvailabilityHistory.id))
            .limit(1)
        )
        return self.session.execute(stmt).scalars().first()

    def list_page(
        self, product_id: int, *, limit: int, offset: int
    ) -> list[AvailabilityHistory]:
        stmt = (
            select(AvailabilityHistory)
            .where(AvailabilityHistory.product_id == product_id)
            .order_by(desc(AvailabilityHistory.observed_at), desc(AvailabilityHistory.id))
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars())

    def count_for_product(self, product_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(AvailabilityHistory)
            .where(AvailabilityHistory.product_id == product_id)
        )
        return int(self.session.execute(stmt).scalar_one())
