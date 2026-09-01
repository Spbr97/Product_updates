"""Price and availability history against a real database.

Covers the append-only guarantee, statistics arithmetic, and the fact that a check only
writes history when it actually learned something.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy.orm import Session
from tests.unit.test_adapters import load

from product_tracker.core.config import get_settings
from product_tracker.db.models import PriceHistory
from product_tracker.domain.enums import Availability
from product_tracker.domain.errors import NotFoundError
from product_tracker.repositories.availability_history import AvailabilityHistoryRepository
from product_tracker.repositories.price_history import PriceHistoryRepository
from product_tracker.services.history_service import HistoryService
from product_tracker.services.product_service import ProductService
from product_tracker.services.tracking import TrackingEngine
from product_tracker.stores.registry import StoreRegistry

pytestmark = pytest.mark.db

BASE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def service(db_session: Session) -> ProductService:
    return ProductService(db_session, StoreRegistry(), get_settings())


@pytest.fixture
def engine() -> TrackingEngine:
    return TrackingEngine(StoreRegistry(), get_settings())


@pytest.fixture
def product_id(service: ProductService) -> int:
    return service.add("https://shop.example.com/p/history").id


def seed(
    session: Session, product_id: int, *series: tuple[str, int], currency: str = "INR"
) -> None:
    """Insert (price, hours-offset) observations directly, bypassing the fetch path."""
    repo = PriceHistoryRepository(session)
    for price, hours in series:
        repo.record(
            product_id=product_id,
            price=Decimal(price),
            currency=currency,
            observed_at=BASE + timedelta(hours=hours),
        )


class TestAppendOnly:
    def test_records_accumulate(self, db_session: Session, product_id: int) -> None:
        seed(db_session, product_id, ("100", 0), ("90", 1), ("95", 2))

        assert PriceHistoryRepository(db_session).count_for_product(product_id) == 3

    def test_latest_is_the_newest_observation(
        self, db_session: Session, product_id: int
    ) -> None:
        seed(db_session, product_id, ("100", 0), ("90", 1), ("95", 2))

        latest = PriceHistoryRepository(db_session).latest(product_id)

        assert latest is not None
        assert latest.price == Decimal("95")

    def test_history_survives_a_later_lower_price(
        self, db_session: Session, product_id: int
    ) -> None:
        """The old value must still be readable; nothing overwrites."""
        seed(db_session, product_id, ("100", 0), ("90", 1))

        prices = {row.price for row in PriceHistoryRepository(db_session).list_page(
            product_id, limit=10, offset=0
        )}

        assert prices == {Decimal("100"), Decimal("90")}

    def test_repository_exposes_no_update_or_delete(self) -> None:
        """Append-only is enforced by the interface, not just by convention."""
        assert not hasattr(PriceHistoryRepository, "update")
        assert not hasattr(AvailabilityHistoryRepository, "update")

    def test_listing_is_newest_first(self, db_session: Session, product_id: int) -> None:
        seed(db_session, product_id, ("100", 0), ("90", 1), ("95", 2))

        rows = PriceHistoryRepository(db_session).list_page(product_id, limit=10, offset=0)

        assert [row.price for row in rows] == [Decimal("95"), Decimal("90"), Decimal("100")]

    def test_deleting_the_product_cascades(
        self, db_session: Session, service: ProductService, product_id: int
    ) -> None:
        seed(db_session, product_id, ("100", 0))

        service.remove(product_id)

        assert PriceHistoryRepository(db_session).count_for_product(product_id) == 0


class TestStatistics:
    def test_none_without_history(self, db_session: Session, product_id: int) -> None:
        assert PriceHistoryRepository(db_session).stats(product_id) is None

    def test_aggregates(self, db_session: Session, product_id: int) -> None:
        seed(db_session, product_id, ("100", 0), ("80", 1), ("120", 2), ("90", 3))

        stats = PriceHistoryRepository(db_session).stats(product_id)

        assert stats is not None
        assert stats.observations == 4
        assert stats.current == Decimal("90")
        assert stats.lowest == Decimal("80")
        assert stats.highest == Decimal("120")
        assert stats.average == Decimal("97.50")
        assert stats.currency == "INR"

    def test_when_the_low_occurred(self, db_session: Session, product_id: int) -> None:
        seed(db_session, product_id, ("100", 0), ("80", 1), ("120", 2))

        stats = PriceHistoryRepository(db_session).stats(product_id)

        assert stats is not None
        assert stats.lowest_at == BASE + timedelta(hours=1)
        assert stats.highest_at == BASE + timedelta(hours=2)
        assert stats.first_observed_at == BASE

    def test_repeated_low_reports_the_first_occurrence(
        self, db_session: Session, product_id: int
    ) -> None:
        """"The lowest price occurred on..." should name when it first hit that low."""
        seed(db_session, product_id, ("80", 0), ("100", 1), ("80", 2))

        stats = PriceHistoryRepository(db_session).stats(product_id)

        assert stats is not None
        assert stats.lowest_at == BASE

    def test_change_since_first_observation(
        self, db_session: Session, product_id: int
    ) -> None:
        seed(db_session, product_id, ("100", 0), ("75", 1))

        stats = PriceHistoryRepository(db_session).stats(product_id)

        assert stats is not None
        assert stats.changed_by == Decimal("-25")
        assert stats.changed_pct == Decimal("-25.00")

    def test_increase_is_positive(self, db_session: Session, product_id: int) -> None:
        seed(db_session, product_id, ("100", 0), ("125", 1))

        stats = PriceHistoryRepository(db_session).stats(product_id)

        assert stats is not None
        assert stats.changed_pct == Decimal("25.00")

    def test_single_observation_has_zero_change(
        self, db_session: Session, product_id: int
    ) -> None:
        seed(db_session, product_id, ("100", 0))

        stats = PriceHistoryRepository(db_session).stats(product_id)

        assert stats is not None
        assert stats.observations == 1
        assert stats.changed_by == Decimal("0")
        assert stats.lowest == stats.highest == stats.current == Decimal("100")

    def test_zero_first_price_does_not_divide_by_zero(
        self, db_session: Session, product_id: int
    ) -> None:
        seed(db_session, product_id, ("0", 0), ("100", 1))

        stats = PriceHistoryRepository(db_session).stats(product_id)

        assert stats is not None
        assert stats.changed_by == Decimal("100")
        assert stats.changed_pct is None


class TestMixedCurrency:
    def test_statistics_cover_only_the_current_currency(
        self, db_session: Session, product_id: int
    ) -> None:
        """Averaging INR with USD would produce a meaningless number."""
        seed(db_session, product_id, ("1000", 0), ("1200", 1), currency="INR")
        seed(db_session, product_id, ("20", 2), ("30", 3), currency="USD")

        stats = PriceHistoryRepository(db_session).stats(product_id)

        assert stats is not None
        assert stats.currency == "USD"
        assert stats.observations == 2
        assert stats.lowest == Decimal("20")
        assert stats.highest == Decimal("30")
        assert stats.mixed_currency is True

    def test_single_currency_is_not_flagged(
        self, db_session: Session, product_id: int
    ) -> None:
        seed(db_session, product_id, ("100", 0), ("90", 1))

        stats = PriceHistoryRepository(db_session).stats(product_id)

        assert stats is not None
        assert stats.mixed_currency is False


@respx.mock
class TestEngineWritesHistory:
    def _stub(self, url: str, html: str) -> None:
        respx.get(url).mock(return_value=httpx.Response(200, html=html))

    def test_first_successful_check_records_both(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://shop.example.com/p/first"
        self._stub(url, load("jsonld_in_stock.html"))
        product = service.add(url)

        execution = engine.check_product(db_session, product.id)

        assert PriceHistoryRepository(db_session).count_for_product(product.id) == 1
        assert AvailabilityHistoryRepository(db_session).count_for_product(product.id) == 1
        # A first observation is recorded but is not a "change".
        assert execution.price_changed is False
        assert execution.availability_changed is False

    def test_unchanged_price_does_not_add_a_row(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://shop.example.com/p/steady"
        self._stub(url, load("jsonld_in_stock.html"))
        product = service.add(url)

        for _ in range(3):
            engine.check_product(db_session, product.id)

        assert PriceHistoryRepository(db_session).count_for_product(product.id) == 1
        assert AvailabilityHistoryRepository(db_session).count_for_product(product.id) == 1

    def test_price_change_is_recorded_and_flagged(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://shop.example.com/p/moving"
        route = respx.get(url)
        route.mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        product = service.add(url)
        engine.check_product(db_session, product.id)

        cheaper = load("jsonld_in_stock.html").replace("69999.00", "59999.00")
        route.mock(return_value=httpx.Response(200, html=cheaper))
        execution = engine.check_product(db_session, product.id)

        assert execution.price_changed is True
        rows = PriceHistoryRepository(db_session).list_page(product.id, limit=10, offset=0)
        assert [row.price for row in rows] == [Decimal("59999.00"), Decimal("69999.00")]

    def test_availability_transition_is_recorded(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://shop.example.com/p/stock-change"
        route = respx.get(url)
        route.mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        product = service.add(url)
        engine.check_product(db_session, product.id)

        route.mock(return_value=httpx.Response(200, html=load("jsonld_out_of_stock.html")))
        execution = engine.check_product(db_session, product.id)

        assert execution.availability_changed is True
        rows = AvailabilityHistoryRepository(db_session).list_page(
            product.id, limit=10, offset=0
        )
        assert [row.availability for row in rows] == [
            Availability.OUT_OF_STOCK,
            Availability.IN_STOCK,
        ]

    def test_failed_check_writes_no_history(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        """A blocked fetch learned nothing; history must stay untouched."""
        url = "https://shop.example.com/p/flaky-history"
        route = respx.get(url)
        route.mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        product = service.add(url)
        engine.check_product(db_session, product.id)

        route.mock(return_value=httpx.Response(403))
        execution = engine.check_product(db_session, product.id)

        assert execution.price_changed is False
        assert execution.availability_changed is False
        assert PriceHistoryRepository(db_session).count_for_product(product.id) == 1
        assert AvailabilityHistoryRepository(db_session).count_for_product(product.id) == 1

    def test_history_rows_reference_the_check_that_produced_them(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://shop.example.com/p/provenance"
        self._stub(url, load("jsonld_in_stock.html"))
        product = service.add(url)

        execution = engine.check_product(db_session, product.id)

        row = PriceHistoryRepository(db_session).latest(product.id)
        assert row is not None
        assert row.check_execution_id == execution.id


class TestHistoryService:
    def test_missing_product_raises(self, db_session: Session) -> None:
        with pytest.raises(NotFoundError):
            HistoryService(db_session).price_history(999_999)

    def test_stats_for_missing_product_raises(self, db_session: Session) -> None:
        with pytest.raises(NotFoundError):
            HistoryService(db_session).stats(999_999)

    def test_empty_history_is_not_an_error(
        self, db_session: Session, product_id: int
    ) -> None:
        """A product with no checks yet is a valid state, not a 404."""
        page = HistoryService(db_session).price_history(product_id)

        assert page.items == []
        assert page.total == 0
        assert HistoryService(db_session).stats(product_id) is None

    def test_pagination(self, db_session: Session, product_id: int) -> None:
        seed(db_session, product_id, *[(str(100 + i), i) for i in range(5)])
        service = HistoryService(db_session)

        first = service.price_history(product_id, limit=2, offset=0)
        second = service.price_history(product_id, limit=2, offset=2)

        assert first.total == second.total == 5
        assert len(first.items) == len(second.items) == 2
        assert {row.id for row in first.items}.isdisjoint({row.id for row in second.items})


class TestSchemaGuards:
    def test_negative_price_is_rejected_by_the_database(
        self, db_session: Session, product_id: int
    ) -> None:
        """A parsing bug must not be able to poison permanent history."""
        from sqlalchemy.exc import IntegrityError

        # In a savepoint: the failed INSERT poisons its transaction, and the outer one
        # still has to roll back cleanly when the fixture tears down.
        with pytest.raises(IntegrityError), db_session.begin_nested():
            db_session.add(
                PriceHistory(
                    product_id=product_id,
                    price=Decimal("-1"),
                    currency="INR",
                    observed_at=BASE,
                )
            )
            db_session.flush()
