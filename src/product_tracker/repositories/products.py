"""Product repository."""

from __future__ import annotations

from sqlalchemy import Select, exists, func, select
from sqlalchemy.orm import joinedload

from ..db.models import Product, Store, Subscription
from ..domain.enums import TrackingStatus
from .base import Repository


class ProductRepository(Repository[Product]):
    model = Product

    def get(self, entity_id: int) -> Product | None:
        stmt = (
            select(Product).options(joinedload(Product.store)).where(Product.id == entity_id)
        )
        return self.session.execute(stmt).unique().scalar_one_or_none()

    def get_by_canonical_url(self, url_canonical: str) -> Product | None:
        """Duplicate detection: the canonical URL carries the uniqueness constraint."""
        stmt = select(Product).where(Product.url_canonical == url_canonical)
        return self.session.execute(stmt).unique().scalar_one_or_none()

    def _filtered(
        self,
        *,
        store_slug: str | None,
        tracking_status: TrackingStatus | None,
        subscriber_id: int | None = None,
    ) -> Select[tuple[Product]]:
        stmt = select(Product)
        if store_slug is not None:
            stmt = stmt.join(Store, Product.store_id == Store.id).where(Store.slug == store_slug)
        if tracking_status is not None:
            stmt = stmt.where(Product.tracking_status == tracking_status)
        if subscriber_id is not None:
            # Listings are shared, so a watchlist is defined by subscription, not by the
            # products table. EXISTS rather than a join: a join would need DISTINCT once a
            # listing has several subscribers, and would quietly change the row count.
            stmt = stmt.where(
                exists().where(
                    Subscription.product_id == Product.id,
                    Subscription.user_id == subscriber_id,
                )
            )
        return stmt

    def list_page(
        self,
        *,
        limit: int,
        offset: int,
        store_slug: str | None = None,
        tracking_status: TrackingStatus | None = None,
        subscriber_id: int | None = None,
    ) -> list[Product]:
        stmt = (
            self._filtered(
                store_slug=store_slug,
                tracking_status=tracking_status,
                subscriber_id=subscriber_id,
            )
            .options(joinedload(Product.store))
            .order_by(Product.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).unique().scalars())

    def count_filtered(
        self,
        *,
        store_slug: str | None = None,
        tracking_status: TrackingStatus | None = None,
        subscriber_id: int | None = None,
    ) -> int:
        inner = self._filtered(
            store_slug=store_slug,
            tracking_status=tracking_status,
            subscriber_id=subscriber_id,
        ).subquery()
        stmt = select(func.count()).select_from(inner)
        return int(self.session.execute(stmt).scalar_one())

    def list_schedulable(self) -> list[Product]:
        """Active products, for the scheduler's reconcile pass."""
        stmt = (
            select(Product)
            .where(Product.tracking_status == TrackingStatus.ACTIVE)
            .order_by(Product.id)
        )
        return list(self.session.execute(stmt).unique().scalars())

    def count_by_status(self) -> dict[TrackingStatus, int]:
        stmt = select(Product.tracking_status, func.count()).group_by(Product.tracking_status)
        counts = dict.fromkeys(TrackingStatus, 0)
        for status, total in self.session.execute(stmt):
            counts[status] = int(total)
        return counts
