"""Product Entries: one logical product, one listing per retailer.

The identity a person keeps. Everything below exists to protect one sentence: *a price
change must never look like a new product.* An entry keeps its id while prices move, while
a retailer URL is replaced, and while one shop stops stocking the thing, because every
observation belongs to the retailer listing that saw it rather than to the entry.

Three decisions are load-bearing:

**Creation is one transaction.** An entry with only an Amazon listing, because the Flipkart
insert failed, is worse than no entry at all -- the user would see a half-built product and
have no way to tell it was half-built. The service flushes; the caller's transaction commits
or rolls back the lot.

**A URL change re-points, it does not rewrite.** The old prices were genuinely observed at
the old URL. So the listing keeps its id and gets a new tracking target, the old target
keeps its history untouched, and the move itself is recorded in an audit row where it
explains a discontinuity without falsifying it.

**Removal is deactivation.** Dropping Amazon leaves the entry and Flipkart exactly as they
were, and leaves Amazon's months of observations readable. The partial unique index is
written against ``deactivated_at IS NULL`` precisely so a dead listing can sit beside its
replacement.

This module owns none of the tracking. It arranges ``products`` rows and hands them to the
engine that already exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.logging import get_logger
from ..db.models import Product, ProductEntry, RetailerListing
from ..domain.enums import ProductEntryStatus, TrackingStatus
from ..domain.errors import (
    DuplicateListingError,
    InvalidStoreURLError,
    NotFoundError,
    ValidationError,
)
from ..repositories.product_entries import (
    ProductEntryRepository,
    RetailerListingRepository,
)
from ..repositories.users import SubscriptionRepository
from ..stores.catalogue import STORES_BY_SLUG, resolve_store
from ..stores.registry import StoreRegistry
from ..utils.urls import canonicalize_url
from .product_service import ProductService

log = get_logger(__name__)

#: The retailers a v1 entry is built from, in the order the form asks for them.
AMAZON_SLUG = "amazon-in"
FLIPKART_SLUG = "flipkart"
ENTRY_STORES: tuple[str, ...] = (AMAZON_SLUG, FLIPKART_SLUG)

MAX_NAME_LENGTH = 200

#: Spelled as an alias because this class has a method called ``list``, which shadows the
#: builtin inside the class body and makes ``list[RetailerListing]`` unresolvable there.
type Listings = list[RetailerListing]
type Ids = list[int]


@dataclass(frozen=True, slots=True)
class ListingInput:
    """One retailer's half of the Add Product form."""

    product_name: str
    url: str


@dataclass(frozen=True, slots=True)
class EntryPage:
    """A page of entries plus the unpaginated total."""

    items: list[ProductEntry]
    total: int
    limit: int
    offset: int


class ProductEntryService:
    """Product Entries for one account. Every method is scoped to ``user_id``.

    An entry belonging to somebody else is reported as *not found* rather than forbidden,
    so sequential ids cannot be walked to learn what exists -- the same rule the rest of
    this project holds to.
    """

    def __init__(
        self,
        session: Session,
        registry: StoreRegistry,
        settings: Settings,
        user_id: int,
    ) -> None:
        self.session = session
        self.settings = settings
        self.user_id = user_id
        self.entries = ProductEntryRepository(session)
        self.listings = RetailerListingRepository(session)
        self.subscriptions = SubscriptionRepository(session)
        #: Reused rather than reimplemented: one place turns a URL into a tracked row.
        self.products = ProductService(session, registry, settings, user_id)

    # --- Creation ----------------------------------------------------------------

    def create(
        self, canonical_name: str, *, amazon: ListingInput, flipkart: ListingInput
    ) -> ProductEntry:
        """Create one entry with one Amazon and one Flipkart listing.

        Validation runs before anything is written, so a rejected form leaves no trace.
        The two listings and the entry are staged together; the caller's transaction is
        what makes them atomic.
        """
        name = self._clean_name(canonical_name, "product name")
        wanted = {
            AMAZON_SLUG: ListingInput(
                self._clean_name(amazon.product_name, "Amazon product name"), amazon.url
            ),
            FLIPKART_SLUG: ListingInput(
                self._clean_name(flipkart.product_name, "Flipkart product name"),
                flipkart.url,
            ),
        }

        # Every URL checked against every rule before the first insert. Validating as we go
        # would let a bad Flipkart URL leave a tracked Amazon product behind.
        for slug, given in wanted.items():
            self._assert_store(slug, given.url)
            self._assert_not_already_listed(given.url)

        entry = ProductEntry(
            user_id=self.user_id,
            canonical_name=name,
            status=ProductEntryStatus.ACTIVE,
        )
        self.entries.add(entry)

        for slug, given in wanted.items():
            product = self.products.track_url(given.url)
            self._add_listing(entry, product, slug, given.product_name)

        self.session.flush()
        log.info(
            "product_entry.created",
            product_entry_id=entry.id,
            user_id=self.user_id,
            listings=len(wanted),
        )
        return entry

    def _add_listing(
        self, entry: ProductEntry, product: Product, store_slug: str, product_name: str
    ) -> RetailerListing:
        listing = RetailerListing(
            product_entry_id=entry.id,
            product_id=product.id,
            store_slug=store_slug,
            product_name=product_name,
        )
        self.listings.add(listing)
        log.info(
            "retailer_listing.created",
            retailer_listing_id=listing.id,
            product_entry_id=entry.id,
            product_id=product.id,
            store=store_slug,
        )
        return listing

    # --- Reading -----------------------------------------------------------------

    def get(self, entry_id: int) -> ProductEntry:
        entry = self.entries.get_for_user(entry_id, self.user_id)
        if entry is None:
            raise NotFoundError("ProductEntry", entry_id)
        return entry

    def list(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        status: ProductEntryStatus | None = None,
    ) -> EntryPage:
        return EntryPage(
            items=self.entries.list_page(
                self.user_id, limit=limit, offset=offset, status=status
            ),
            total=self.entries.count_filtered(self.user_id, status=status),
            limit=limit,
            offset=offset,
        )

    def active_listings(self, entry_id: int) -> Listings:
        """The live listings of an entry the caller owns."""
        self.get(entry_id)
        return self.listings.active_for_entry(entry_id)

    # --- Updating ----------------------------------------------------------------

    def update(self, entry_id: int, *, canonical_name: str) -> ProductEntry:
        """Rename an entry. Its id, listings and history are untouched."""
        entry = self.get(entry_id)
        entry.canonical_name = self._clean_name(canonical_name, "product name")
        self.session.flush()
        log.info("product_entry.updated", product_entry_id=entry.id, field="canonical_name")
        return entry

    def update_listing(
        self,
        entry_id: int,
        listing_id: int,
        *,
        product_name: str | None = None,
        url: str | None = None,
    ) -> RetailerListing:
        """Change a listing's display name, its URL, or both.

        A name change is metadata and touches nothing else. A URL change re-points the
        listing at a new tracking target: the listing keeps its id, the *old* product row
        and every observation on it stay exactly as they were, and the move is recorded in
        an audit row. Nothing is merged -- if the new URL turns out to be a different
        product, that is the user's to correct, and inventing a merge here would silently
        splice two products' price series together.
        """
        entry = self.get(entry_id)
        listing = self.listings.get_for_entry(listing_id, entry_id)
        if listing is None:
            raise NotFoundError("RetailerListing", listing_id)
        if not listing.is_active:
            raise ValidationError(
                "this listing has been removed; add the retailer again rather than "
                "editing a listing that is no longer tracked"
            )

        if product_name is not None:
            listing.product_name = self._clean_name(product_name, "product name")
            log.info(
                "retailer_listing.updated",
                retailer_listing_id=listing.id,
                field="product_name",
            )

        if url is not None:
            self._repoint(entry, listing, url)

        self.session.flush()
        return listing

    def _repoint(self, entry: ProductEntry, listing: RetailerListing, url: str) -> None:
        """Move a listing to a new URL at the same retailer."""
        # Same shop: an entry's Amazon row must stay Amazon, or the comparison column it
        # feeds would quietly start showing someone else's prices.
        self._assert_store(listing.store_slug, url)

        old = self.session.get(Product, listing.product_id)
        old_url = old.url if old is not None else ""
        if old is not None and canonicalize_url(url) == old.url_canonical:
            return  # Same listing; nothing moved.

        self._assert_not_already_listed(url, ignore_listing_id=listing.id)

        product = self.products.track_url(url)
        listing.product_id = product.id
        self.listings.record_url_change(listing, old_url=old_url, new_url=product.url)

        # The old target is no longer this entry's business. Unsubscribing is what lets it
        # be cleaned up once nobody watches it; its history stays until then, and stays
        # readable through the audit row either way.
        if old is not None and old.id != product.id:
            self._release(old.id)

        log.info(
            "retailer_listing.updated",
            retailer_listing_id=listing.id,
            product_entry_id=entry.id,
            field="url",
            from_product_id=old.id if old is not None else None,
            to_product_id=product.id,
        )

    # --- Removing ----------------------------------------------------------------

    def deactivate_listing(self, entry_id: int, listing_id: int) -> RetailerListing:
        """Stop tracking one retailer. The entry and the other retailer are unaffected.

        Soft, so the observations already recorded stay readable, and so the partial unique
        index frees ``(entry, store)`` for a replacement listing.
        """
        self.get(entry_id)
        listing = self.listings.get_for_entry(listing_id, entry_id)
        if listing is None:
            raise NotFoundError("RetailerListing", listing_id)
        if listing.is_active:
            listing.deactivated_at = datetime.now(UTC)
            self._release(listing.product_id)
            self.session.flush()
            log.info(
                "retailer_listing.deactivated",
                retailer_listing_id=listing.id,
                product_entry_id=entry_id,
                store=listing.store_slug,
            )
        return listing

    def archive(self, entry_id: int) -> ProductEntry:
        """Retire an entry and every listing under it, keeping all of it readable."""
        entry = self.get(entry_id)
        for listing in self.listings.active_for_entry(entry_id):
            self.deactivate_listing(entry_id, listing.id)
        entry.status = ProductEntryStatus.ARCHIVED
        entry.deleted_at = datetime.now(UTC)
        self.session.flush()
        log.info("product_entry.archived", product_entry_id=entry.id, user_id=self.user_id)
        return entry

    # --- Tracking state ----------------------------------------------------------

    def set_tracking(self, entry_id: int, *, active: bool) -> Listings:
        """Pause or resume every live listing of an entry.

        Written on the product row because that is what the scheduler reads: reconcile
        selects on ``tracking_status == ACTIVE``, so this is the switch it actually obeys.
        """
        self.get(entry_id)
        listings = self.listings.active_for_entry(entry_id)
        status = TrackingStatus.ACTIVE if active else TrackingStatus.PAUSED
        for listing in listings:
            product = self.session.get(Product, listing.product_id)
            if product is not None:
                product.tracking_status = status
        self.session.flush()
        log.info(
            "product_entry.tracking",
            product_entry_id=entry_id,
            status=status.value,
            listings=len(listings),
        )
        return listings

    def product_ids(self, entry_id: int, *, listing_id: int | None = None) -> Ids:
        """The tracking targets a check should run against.

        One retailer when ``listing_id`` is given, otherwise every live listing. Returned as
        plain ids because the check runner owns its own sessions and transactions.
        """
        self.get(entry_id)
        if listing_id is not None:
            listing = self.listings.get_for_entry(listing_id, entry_id)
            if listing is None:
                raise NotFoundError("RetailerListing", listing_id)
            return [listing.product_id]
        return [listing.product_id for listing in self.listings.active_for_entry(entry_id)]

    # --- Helpers -----------------------------------------------------------------

    @staticmethod
    def _clean_name(value: str, field: str) -> str:
        name = (value or "").strip()
        if not name:
            raise ValidationError(f"{field} is required")
        if len(name) > MAX_NAME_LENGTH:
            raise ValidationError(f"{field} is longer than {MAX_NAME_LENGTH} characters")
        return name

    @staticmethod
    def _assert_store(expected_slug: str, url: str) -> None:
        """The URL must belong to the retailer whose field it was typed into.

        Checked on the *domain*, never on the retailer-supplied product name: a name is
        something a person typed and proves nothing about where the link goes.
        """
        actual = resolve_store(url)
        if actual.slug != expected_slug:
            expected = STORES_BY_SLUG[expected_slug].display_name
            raise InvalidStoreURLError(expected, actual.display_name, url)

    def _assert_not_already_listed(
        self, url: str, *, ignore_listing_id: int | None = None
    ) -> None:
        """Refuse a URL that is already live in one of this user's entries.

        Deterministic and named: the message says which entry it clashes with. Another
        *user* tracking the same public URL is not a conflict -- listings are per account.
        """
        canonical = canonicalize_url(url)
        clash = self.listings.find_active_by_canonical_url(self.user_id, canonical)
        if clash is not None and clash.id != ignore_listing_id:
            raise DuplicateListingError(url, clash.product_entry_id)

    def _release(self, product_id: int) -> None:
        """Drop this account's subscription to a tracking target it no longer wants.

        The product row itself is left alone: it may be shared with another account, and
        deleting rows other people watch is the failure this whole subscription model
        exists to prevent.
        """
        from .user_service import unsubscribe

        unsubscribe(self.session, self.user_id, product_id)
        remaining = self.subscriptions.subscriber_count(product_id)
        if not remaining:
            product = self.session.get(Product, product_id)
            if product is not None:
                # Nobody is watching: stop the scheduler picking it up. Not deleted --
                # its history is still referenced by this entry's audit trail.
                product.tracking_status = TrackingStatus.PAUSED
