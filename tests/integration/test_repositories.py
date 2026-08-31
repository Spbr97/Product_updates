"""Repository behaviour against a real database."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from product_tracker.db.models import Product, Store
from product_tracker.domain.enums import Availability, TrackingStatus
from product_tracker.domain.models import StoreInfo
from product_tracker.repositories.products import ProductRepository
from product_tracker.repositories.stores import StoreRepository

pytestmark = pytest.mark.db


def make_store(session: Session, slug: str = "example") -> Store:
    store = Store(
        slug=slug, name=slug.title(), domains=[f"{slug}.com"], adapter_key="generic", enabled=True
    )
    session.add(store)
    session.flush()
    return store


def make_product(session: Session, store: Store, url: str) -> Product:
    product = Product(url=url, url_canonical=url, store_id=store.id)
    session.add(product)
    session.flush()
    return product


class TestStoreRepository:
    def test_seeded_stores_are_present(self, db_session: Session) -> None:
        repo = StoreRepository(db_session)
        assert repo.get_by_slug("generic") is not None

    def test_sync_creates_new_stores(self, db_session: Session) -> None:
        repo = StoreRepository(db_session)
        created, _ = repo.sync_from_registry(
            [StoreInfo("amazon-in", "Amazon India", ("amazon.in",), "amazon")]
        )

        assert created == 1
        assert repo.get_by_slug("amazon-in") is not None

    def test_sync_is_idempotent(self, db_session: Session) -> None:
        repo = StoreRepository(db_session)
        info = StoreInfo("croma", "Croma", ("croma.com",), "generic")

        repo.sync_from_registry([info])
        created, updated = repo.sync_from_registry([info])

        assert (created, updated) == (0, 0)

    def test_sync_updates_changed_metadata(self, db_session: Session) -> None:
        repo = StoreRepository(db_session)
        repo.sync_from_registry([StoreInfo("vijay", "Vijay", ("vijaysales.com",), "generic")])

        _, updated = repo.sync_from_registry(
            [StoreInfo("vijay", "Vijay Sales", ("vijaysales.com", "www.vijaysales.com"), "generic")]
        )

        store = repo.get_by_slug("vijay")
        assert updated == 1
        assert store is not None
        assert store.name == "Vijay Sales"
        assert "www.vijaysales.com" in store.domains

    def test_sync_never_deletes(self, db_session: Session) -> None:
        """Retiring a store from code must not orphan products that reference it."""
        repo = StoreRepository(db_session)
        repo.sync_from_registry([StoreInfo("retired", "Retired", ("retired.com",), "generic")])

        repo.sync_from_registry([])

        assert repo.get_by_slug("retired") is not None


class TestProductRepository:
    def test_lookup_by_canonical_url(self, db_session: Session) -> None:
        store = make_store(db_session)
        make_product(db_session, store, "https://example.com/p/1")
        repo = ProductRepository(db_session)

        assert repo.get_by_canonical_url("https://example.com/p/1") is not None
        assert repo.get_by_canonical_url("https://example.com/p/2") is None

    def test_defaults_are_applied(self, db_session: Session) -> None:
        store = make_store(db_session)
        product = make_product(db_session, store, "https://example.com/p/defaults")
        db_session.refresh(product)

        assert product.availability is Availability.UNKNOWN
        assert product.tracking_status is TrackingStatus.ACTIVE
        assert product.consecutive_failures == 0
        assert product.extra_metadata == {}

    def test_pagination_slices_deterministically(self, db_session: Session) -> None:
        store = make_store(db_session)
        for index in range(5):
            make_product(db_session, store, f"https://example.com/page/{index}")
        repo = ProductRepository(db_session)

        first = repo.list_page(limit=2, offset=0)
        second = repo.list_page(limit=2, offset=2)

        assert len(first) == len(second) == 2
        assert {p.id for p in first}.isdisjoint({p.id for p in second})

    def test_count_and_filter_agree(self, db_session: Session) -> None:
        store = make_store(db_session, "filtered")
        for index in range(3):
            make_product(db_session, store, f"https://filtered.com/{index}")
        repo = ProductRepository(db_session)

        assert repo.count_filtered(store_slug="filtered") == 3
        assert repo.count_filtered(store_slug="nonexistent") == 0

    def test_schedulable_excludes_paused(self, db_session: Session) -> None:
        store = make_store(db_session, "sched")
        active = make_product(db_session, store, "https://sched.com/active")
        paused = make_product(db_session, store, "https://sched.com/paused")
        paused.tracking_status = TrackingStatus.PAUSED
        db_session.flush()

        ids = {p.id for p in ProductRepository(db_session).list_schedulable()}

        assert active.id in ids
        assert paused.id not in ids

    def test_count_by_status_covers_every_status(self, db_session: Session) -> None:
        counts = ProductRepository(db_session).count_by_status()
        assert set(counts) == set(TrackingStatus)
