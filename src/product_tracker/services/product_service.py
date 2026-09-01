"""Product management: adding, listing, and removing tracked products.

Where URL policy is enforced. ``add`` is the only entry point that turns an arbitrary
user-supplied string into a row, so validation, the SSRF guard, canonicalisation, and
duplicate detection all live here rather than being repeated in the CLI and the API.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.logging import get_logger
from ..db.models import Product, Store, Subscription
from ..domain.enums import TrackingStatus
from ..domain.errors import DuplicateError, NotFoundError
from ..repositories.products import ProductRepository
from ..repositories.stores import StoreRepository
from ..repositories.users import SubscriptionRepository
from ..stores.catalogue import resolve_store
from ..stores.registry import StoreRegistry
from ..utils.urls import canonicalize_url, host_of, validate_url
from .user_service import assert_subscribed, default_user, unsubscribe

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProductPage:
    """One page of products plus the unpaginated total."""

    items: list[Product]
    total: int
    limit: int
    offset: int


class ProductService:
    def __init__(
        self,
        session: Session,
        registry: StoreRegistry,
        settings: Settings,
        user_id: int | None = None,
    ) -> None:
        self.session = session
        self.registry = registry
        self.settings = settings
        self.user_id = user_id
        self.products = ProductRepository(session)
        self.stores = StoreRepository(session)
        self.subscriptions = SubscriptionRepository(session)

    def add(self, url: str, *, check_interval_seconds: int | None = None) -> Product:
        """Track a product, and subscribe this user to it.

        A listing is shared. If somebody else already tracks this URL, the caller is
        subscribed to the existing row rather than being refused -- they inherit its whole
        price history immediately, and the retailer is still fetched exactly once however
        many people watch it. Only re-adding a listing *this* user already watches is a
        duplicate.

        Raises ``InvalidURLError``/``UnsafeURLError`` for a URL we will not fetch, and
        ``NotFoundError`` if the resolved store has no database row (run
        ``product-tracker stores sync``).
        """
        validated = validate_url(
            url,
            allowed_schemes=self.settings.url_schemes,
            block_private=self.settings.block_private_addresses,
            max_length=self.settings.max_url_length,
        )
        canonical = canonicalize_url(validated)

        existing = self.products.get_by_canonical_url(canonical)
        if existing is not None:
            owner = self._owner_id()
            if self.subscriptions.is_subscribed(owner, existing.id):
                raise DuplicateError("Product", canonical)
            # Somebody else already tracks it: join them on the same row.
            self._subscribe(existing)
            log.info("product.subscribed", product_id=existing.id, user_id=owner)
            return existing

        # Two separate questions: which retailer is this (by domain), and which adapter
        # reads it. Several named stores share the generic adapter.
        store_info = resolve_store(validated)
        adapter = self.registry.resolve(validated)
        store = self._store_row(store_info.slug)

        product = Product(
            url=validated,
            url_canonical=canonical,
            store_id=store.id,
            check_interval_seconds=check_interval_seconds,
            tracking_status=TrackingStatus.ACTIVE,
        )
        self.products.add(product)
        self._subscribe(product)

        log.info(
            "product.added",
            product_id=product.id,
            store=store_info.slug,
            adapter=adapter.slug,
            url_host=host_of(validated),
        )
        return product

    def _owner_id(self) -> int:
        """The account acting. Falls back to the default one for unattributed callers."""
        if self.user_id is not None:
            return self.user_id
        return int(default_user(self.session).id)

    def _subscribe(self, product: Product) -> None:
        owner = self._owner_id()
        if not self.subscriptions.is_subscribed(owner, product.id):
            self.subscriptions.add(Subscription(user_id=owner, product_id=product.id))

    def _store_row(self, slug: str) -> Store:
        store = self.stores.get_by_slug(slug)
        if store is None:
            raise NotFoundError(
                "Store",
                f"{slug} (known store with no database row; run 'product-tracker stores sync')",
            )
        return store

    def get(self, product_id: int) -> Product:
        """One listing, if this user watches it.

        Scoped whenever the service was built for a user. Internal callers -- the tracking
        engine, the scheduler -- construct it without one and go through the repository
        directly, because a check runs on behalf of every subscriber at once.
        """
        product = self.products.get(product_id)
        if product is None:
            raise NotFoundError("Product", product_id)
        if self.user_id is not None:
            assert_subscribed(self.session, self.user_id, product_id)
        return product

    def remove(self, product_id: int) -> None:
        """Stop watching a listing, and delete it once nobody watches it.

        The listing and its history are shared, so removing it outright would destroy
        another user's tracking and months of their observations. This unsubscribes the
        caller; the row itself goes only when the last subscriber leaves, at which point
        history, rules and executions cascade with it as before.
        """
        product = self.get(product_id)
        owner = self._owner_id()
        unsubscribed = unsubscribe(self.session, owner, product_id)

        remaining = self.subscriptions.subscriber_count(product_id)
        if remaining:
            log.info(
                "product.unsubscribed",
                product_id=product_id,
                user_id=owner,
                remaining_subscribers=remaining,
            )
            return

        self.products.delete(product)
        log.info(
            "product.removed",
            product_id=product_id,
            user_id=owner,
            was_subscribed=unsubscribed,
        )

    def list(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        store_slug: str | None = None,
        tracking_status: TrackingStatus | None = None,
    ) -> ProductPage:
        """This user's watchlist.

        Scoped to their subscriptions: the listings table is shared, so an unscoped list
        would show everyone every URL anyone had ever tracked.
        """
        owner = self._owner_id()
        items = self.products.list_page(
            limit=limit,
            offset=offset,
            store_slug=store_slug,
            tracking_status=tracking_status,
            subscriber_id=owner,
        )
        total = self.products.count_filtered(
            store_slug=store_slug, tracking_status=tracking_status, subscriber_id=owner
        )
        return ProductPage(items=items, total=total, limit=limit, offset=offset)
