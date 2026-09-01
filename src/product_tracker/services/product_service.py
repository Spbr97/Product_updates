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
from ..db.models import Product, Store
from ..domain.enums import TrackingStatus
from ..domain.errors import DuplicateError, NotFoundError
from ..repositories.products import ProductRepository
from ..repositories.stores import StoreRepository
from ..stores.catalogue import resolve_store
from ..stores.registry import StoreRegistry
from ..utils.urls import canonicalize_url, host_of, validate_url

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProductPage:
    """One page of products plus the unpaginated total."""

    items: list[Product]
    total: int
    limit: int
    offset: int


class ProductService:
    def __init__(self, session: Session, registry: StoreRegistry, settings: Settings) -> None:
        self.session = session
        self.registry = registry
        self.settings = settings
        self.products = ProductRepository(session)
        self.stores = StoreRepository(session)

    def add(self, url: str, *, check_interval_seconds: int | None = None) -> Product:
        """Register a product for tracking.

        Raises ``InvalidURLError``/``UnsafeURLError`` for a URL we will not fetch,
        ``DuplicateError`` if the same listing is already tracked, and ``NotFoundError``
        if the resolved store has no database row (run ``product-tracker stores sync``).
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
            raise DuplicateError("Product", canonical)

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

        log.info(
            "product.added",
            product_id=product.id,
            store=store_info.slug,
            adapter=adapter.slug,
            url_host=host_of(validated),
        )
        return product

    def _store_row(self, slug: str) -> Store:
        store = self.stores.get_by_slug(slug)
        if store is None:
            raise NotFoundError(
                "Store",
                f"{slug} (known store with no database row; run 'product-tracker stores sync')",
            )
        return store

    def get(self, product_id: int) -> Product:
        product = self.products.get(product_id)
        if product is None:
            raise NotFoundError("Product", product_id)
        return product

    def remove(self, product_id: int) -> None:
        """Delete a product. History, rules, and executions cascade with it."""
        product = self.get(product_id)
        self.products.delete(product)
        log.info("product.removed", product_id=product_id)

    def list(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        store_slug: str | None = None,
        tracking_status: TrackingStatus | None = None,
    ) -> ProductPage:
        items = self.products.list_page(
            limit=limit, offset=offset, store_slug=store_slug, tracking_status=tracking_status
        )
        total = self.products.count_filtered(
            store_slug=store_slug, tracking_status=tracking_status
        )
        return ProductPage(items=items, total=total, limit=limit, offset=offset)
