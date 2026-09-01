"""Data access for product groups and their variants.

The one thing worth knowing here: :meth:`GroupRepository.load_grid` reads an entire
comparison grid in a **fixed three queries**, whatever its size. The obvious implementation
walks ``group.variants`` and then ``variant.products`` and issues a query per cell, which
looks fine against the four rows in a test and falls over against a real catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from ..db.models import (
    CheckExecution,
    PriceHistory,
    Product,
    ProductGroup,
    ProductVariant,
    VariantListing,
)
from .base import Repository


@dataclass(frozen=True, slots=True)
class GridData:
    """Everything the comparison service needs, already loaded."""

    group: ProductGroup
    variants: list[ProductVariant]
    products_by_variant: dict[int, list[Product]]
    #: product_id -> the price recorded *before* the current one, for movement arrows.
    previous_price: dict[int, Decimal]
    #: product_id -> (status, error_type) of the most recent check. This is what lets a
    #: cell say "blocked" rather than blankly showing no price, which would imply the
    #: shop had told us something about the product when it had told us nothing.
    last_check: dict[int, tuple[str, str | None]]


class GroupRepository(Repository[ProductGroup]):
    model = ProductGroup

    def get_by_slug(self, user_id: int, slug: str) -> ProductGroup | None:
        """One user's group. Scoped by owner, so a slug someone else owns simply does not
        exist from here -- which is what makes a 404, rather than a 403, the honest answer."""
        stmt = select(ProductGroup).where(
            ProductGroup.user_id == user_id, ProductGroup.slug == slug
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_all(self, user_id: int) -> list[ProductGroup]:
        stmt = (
            select(ProductGroup)
            .where(ProductGroup.user_id == user_id)
            .options(selectinload(ProductGroup.variants))
            .order_by(ProductGroup.name)
        )
        return list(self.session.execute(stmt).scalars().unique())

    def listing_counts(self, user_id: int) -> dict[int, int]:
        """group_id -> number of attached listings, in one query."""
        stmt = (
            select(ProductVariant.group_id, func.count(Product.id))
            .join(VariantListing, VariantListing.variant_id == ProductVariant.id)
            .join(Product, Product.id == VariantListing.product_id)
            .join(ProductGroup, ProductGroup.id == ProductVariant.group_id)
            .where(ProductGroup.user_id == user_id)
            .group_by(ProductVariant.group_id)
        )
        return {row[0]: int(row[1]) for row in self.session.execute(stmt)}

    def load_grid(self, group: ProductGroup) -> GridData:
        """Load a whole group's grid in three queries."""
        variants = list(
            self.session.execute(
                select(ProductVariant)
                .where(ProductVariant.group_id == group.id)
                .order_by(ProductVariant.position, ProductVariant.label)
            )
            .scalars()
            .all()
        )
        variant_ids = [variant.id for variant in variants]
        if not variant_ids:
            return GridData(group, variants, {}, {}, {})

        rows = list(
            self.session.execute(
                select(VariantListing.variant_id, Product)
                .join(Product, Product.id == VariantListing.product_id)
                .where(VariantListing.variant_id.in_(variant_ids))
                .options(joinedload(Product.store))
                .order_by(Product.id)
            )
            .unique()
            .all()
        )

        by_variant: dict[int, list[Product]] = {variant_id: [] for variant_id in variant_ids}
        seen: dict[int, Product] = {}
        for variant_id, product in rows:
            by_variant[variant_id].append(product)
            seen[product.id] = product
        products = list(seen.values())

        return GridData(
            group,
            variants,
            by_variant,
            self._previous_prices(products),
            self._last_checks(products),
        )

    def _previous_prices(self, products: list[Product]) -> dict[int, Decimal]:
        """The second-most-recent price per product, in one window-function query.

        History is append-only and only records *changes*, so "the row before the latest"
        is genuinely the previous price rather than a repeat of the current one.
        """
        product_ids = [product.id for product in products]
        if not product_ids:
            return {}

        ranked = (
            select(
                PriceHistory.product_id,
                PriceHistory.price,
                func.row_number()
                .over(
                    partition_by=PriceHistory.product_id,
                    order_by=PriceHistory.observed_at.desc(),
                )
                .label("rank"),
            )
            .where(PriceHistory.product_id.in_(product_ids))
            .subquery()
        )
        stmt = select(ranked.c.product_id, ranked.c.price).where(ranked.c.rank == 2)
        return {row[0]: row[1] for row in self.session.execute(stmt)}


    def _last_checks(self, products: list[Product]) -> dict[int, tuple[str, str | None]]:
        """The most recent check per product: its status and failure reason, in one query."""
        product_ids = [product.id for product in products]
        if not product_ids:
            return {}

        ranked = (
            select(
                CheckExecution.product_id,
                CheckExecution.status,
                CheckExecution.error_type,
                func.row_number()
                .over(
                    partition_by=CheckExecution.product_id,
                    order_by=CheckExecution.started_at.desc(),
                )
                .label("rank"),
            )
            .where(CheckExecution.product_id.in_(product_ids))
            .subquery()
        )
        stmt = select(ranked.c.product_id, ranked.c.status, ranked.c.error_type).where(
            ranked.c.rank == 1
        )
        return {row[0]: (str(row[1]), row[2]) for row in self.session.execute(stmt)}


class VariantRepository(Repository[ProductVariant]):
    model = ProductVariant

    def get_by_label(self, group_id: int, label: str) -> ProductVariant | None:
        stmt = select(ProductVariant).where(
            ProductVariant.group_id == group_id, ProductVariant.label == label
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_for_group(self, group_id: int) -> list[ProductVariant]:
        stmt = (
            select(ProductVariant)
            .where(ProductVariant.group_id == group_id)
            .order_by(ProductVariant.position, ProductVariant.label)
        )
        return list(self.session.execute(stmt).scalars().all())
