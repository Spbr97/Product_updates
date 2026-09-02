"""Data access for Product Entries and their retailer listings.

Every read here is scoped by owner. An entry belonging to somebody else does not come back
as forbidden, it does not come back at all -- which is what lets the service answer 404
rather than 403, so sequential ids cannot be walked to learn what exists.

Like every repository in this project, nothing below commits: the caller owns the
transaction, so creating an entry and both of its listings is one unit of work that either
lands whole or not at all.
"""

from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.orm import selectinload

from ..db.models import Product, ProductEntry, RetailerListing, RetailerListingUrlAudit
from ..domain.enums import ProductEntryStatus
from .base import Repository


class ProductEntryRepository(Repository[ProductEntry]):
    model = ProductEntry

    def get_for_user(self, entry_id: int, user_id: int) -> ProductEntry | None:
        """One user's entry, with its listings loaded.

        Scoped by owner for the reason above. ``selectinload`` because every caller goes on
        to read the listings, and lazy-loading them would issue a query per entry.
        """
        stmt = (
            select(ProductEntry)
            .where(ProductEntry.id == entry_id, ProductEntry.user_id == user_id)
            .options(selectinload(ProductEntry.listings).joinedload(RetailerListing.product))
        )
        return self.session.execute(stmt).unique().scalar_one_or_none()

    def list_page(
        self,
        user_id: int,
        *,
        limit: int,
        offset: int,
        status: ProductEntryStatus | None = None,
    ) -> list[ProductEntry]:
        stmt = (
            self._scoped(user_id, status)
            .options(selectinload(ProductEntry.listings).joinedload(RetailerListing.product))
            .order_by(ProductEntry.created_at.desc(), ProductEntry.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).unique().scalars().all())

    def count_filtered(
        self, user_id: int, *, status: ProductEntryStatus | None = None
    ) -> int:
        stmt = select(func.count()).select_from(self._scoped(user_id, status).subquery())
        return int(self.session.execute(stmt).scalar_one())

    @staticmethod
    def _scoped(
        user_id: int, status: ProductEntryStatus | None
    ) -> Select[tuple[ProductEntry]]:
        stmt = select(ProductEntry).where(ProductEntry.user_id == user_id)
        if status is not None:
            stmt = stmt.where(ProductEntry.status == status)
        return stmt


class RetailerListingRepository(Repository[RetailerListing]):
    model = RetailerListing

    def get_for_entry(self, listing_id: int, entry_id: int) -> RetailerListing | None:
        """A listing, only if it belongs to the entry the caller named.

        Checking the parent rather than trusting the listing id is what stops
        ``/product-entries/1/listings/999`` reaching somebody else's listing: ownership was
        established on the entry, and this keeps the listing inside it.
        """
        stmt = select(RetailerListing).where(
            RetailerListing.id == listing_id,
            RetailerListing.product_entry_id == entry_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def active_for_entry(self, entry_id: int) -> list[RetailerListing]:
        stmt = (
            select(RetailerListing)
            .where(
                RetailerListing.product_entry_id == entry_id,
                RetailerListing.deactivated_at.is_(None),
            )
            .order_by(RetailerListing.store_slug)
        )
        return list(self.session.execute(stmt).scalars().all())

    def active_for_store(self, entry_id: int, store_slug: str) -> RetailerListing | None:
        """The live listing for one retailer, which the partial unique index makes at most
        one. Used to reject a second Amazon listing before the database has to."""
        stmt = select(RetailerListing).where(
            RetailerListing.product_entry_id == entry_id,
            RetailerListing.store_slug == store_slug,
            RetailerListing.deactivated_at.is_(None),
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def find_active_by_canonical_url(
        self, user_id: int, url_canonical: str
    ) -> RetailerListing | None:
        """Whether this URL is already live in one of this user's entries.

        The join through ``products`` is what makes the answer about the *listing* rather
        than the URL: the same public URL may be tracked by several people, and only a
        clash inside one account is a conflict worth refusing.
        """
        stmt = (
            select(RetailerListing)
            .join(Product, Product.id == RetailerListing.product_id)
            .join(ProductEntry, ProductEntry.id == RetailerListing.product_entry_id)
            .where(
                ProductEntry.user_id == user_id,
                Product.url_canonical == url_canonical,
                RetailerListing.deactivated_at.is_(None),
            )
        )
        return self.session.execute(stmt).scalars().first()

    def record_url_change(
        self, listing: RetailerListing, *, old_url: str, new_url: str
    ) -> RetailerListingUrlAudit:
        """Note that a listing moved, without touching a single price observation."""
        audit = RetailerListingUrlAudit(
            retailer_listing_id=listing.id, old_url=old_url, new_url=new_url
        )
        self.session.add(audit)
        self.session.flush()
        return audit
