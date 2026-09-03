"""ProductService against a real database."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from sqlalchemy.orm import Session
from tests.unit.test_adapters import load

from product_tracker.core.config import get_settings
from product_tracker.domain.enums import Availability, CheckStatus, TrackingStatus
from product_tracker.domain.errors import (
    DuplicateError,
    InvalidURLError,
    NotFoundError,
    UnsafeURLError,
)
from product_tracker.repositories.executions import CheckExecutionRepository
from product_tracker.services.product_service import ProductService
from product_tracker.services.tracking import TrackingEngine
from product_tracker.stores.registry import StoreRegistry

pytestmark = pytest.mark.db

FLIPKART_URL = "https://www.flipkart.com/apple-iphone-17/p/itm1?pid=MOBABC123"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    """Activate respx for every test in this module.

    Not ``@respx.mock`` on the class: in respx 0.23 that decorator returns a *function*,
    so pytest silently stops collecting the class and the tests never run.
    """
    with respx.mock:
        yield


@pytest.fixture
def service(db_session: Session) -> ProductService:
    return ProductService(db_session, StoreRegistry(), get_settings())


@pytest.fixture
def engine(db_env: None) -> TrackingEngine:
    """Depends on db_env so settings are configured whatever order fixtures resolve in."""
    return TrackingEngine(StoreRegistry(), get_settings())


class TestAdd:
    def test_adds_a_product_and_resolves_its_store(self, service: ProductService) -> None:
        product = service.add(FLIPKART_URL)

        assert product.id is not None
        assert product.store.slug == "flipkart"
        assert product.tracking_status is TrackingStatus.ACTIVE
        assert product.availability is Availability.UNKNOWN

    def test_unknown_site_falls_back_to_the_generic_adapter(
        self, service: ProductService
    ) -> None:
        product = service.add("https://some-shop.example.com/p/1")
        assert product.store.slug == "generic"

    def test_stores_the_canonical_url(self, service: ProductService) -> None:
        product = service.add(f"{FLIPKART_URL}&utm_source=share&lid=LSTXYZ")

        assert "utm_source" not in product.url_canonical
        assert "lid=" not in product.url_canonical
        assert "pid=MOBABC123" in product.url_canonical

    def test_same_listing_twice_is_rejected(self, service: ProductService) -> None:
        service.add(FLIPKART_URL)

        with pytest.raises(DuplicateError):
            service.add(FLIPKART_URL)

    def test_duplicate_detection_survives_tracking_parameters(
        self, service: ProductService
    ) -> None:
        """The same product shared from two places must not be tracked twice."""
        service.add(f"{FLIPKART_URL}&utm_source=whatsapp")

        with pytest.raises(DuplicateError):
            service.add(f"{FLIPKART_URL}&otracker=search&fm=organic")

    def test_different_variants_are_separate_products(self, service: ProductService) -> None:
        first = service.add("https://www.flipkart.com/x/p/itm1?pid=BLACK256")
        second = service.add("https://www.flipkart.com/x/p/itm1?pid=BLUE256")

        assert first.id != second.id

    @pytest.mark.parametrize(
        "url", ["ftp://example.com/f", "not-a-url", "https:///nohost", ""]
    )
    def test_rejects_invalid_urls(self, service: ProductService, url: str) -> None:
        with pytest.raises(InvalidURLError):
            service.add(url)

    def test_rejects_internal_addresses(
        self, db_session: Session, strict_url_policy: None
    ) -> None:
        """Built after enabling the guard, so the service picks up the strict policy."""
        strict = ProductService(db_session, StoreRegistry(), get_settings())

        with pytest.raises(UnsafeURLError):
            strict.add("http://169.254.169.254/latest/meta-data/")

    def test_accepts_a_custom_interval(self, service: ProductService) -> None:
        product = service.add(FLIPKART_URL, check_interval_seconds=900)
        assert product.check_interval_seconds == 900


class TestGetListRemove:
    def test_get_missing_product_raises(self, service: ProductService) -> None:
        with pytest.raises(NotFoundError):
            service.get(999_999)

    def test_list_paginates_and_totals(self, service: ProductService) -> None:
        for index in range(5):
            service.add(f"https://shop.example.com/p/{index}")

        page = service.list(limit=2, offset=0)

        assert len(page.items) == 2
        assert page.total == 5

    def test_list_filters_by_store(self, service: ProductService) -> None:
        service.add(FLIPKART_URL)
        service.add("https://shop.example.com/p/other")

        assert service.list(store_slug="flipkart").total == 1
        assert service.list(store_slug="generic").total == 1

    def test_remove_deletes(self, service: ProductService) -> None:
        product = service.add(FLIPKART_URL)
        product_id = product.id

        service.remove(product_id)

        with pytest.raises(NotFoundError):
            service.get(product_id)

    def test_remove_missing_raises(self, service: ProductService) -> None:
        with pytest.raises(NotFoundError):
            service.remove(999_999)

    def test_readding_after_removal_is_allowed(self, service: ProductService) -> None:
        service.remove(service.add(FLIPKART_URL).id)
        assert service.add(FLIPKART_URL) is not None


class TestCheckProduct:
    """The engine writes an execution row for every check, success or failure."""

    def test_successful_check_updates_the_product(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://shop.example.com/p/success"
        respx.get(url).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )
        product = service.add(url)

        execution = engine.check_product(db_session, product.id)

        assert execution.status is CheckStatus.SUCCESS
        assert str(execution.extracted_price) == "69999.00"
        assert execution.availability_result is Availability.IN_STOCK
        assert product.name == "Apple iPhone 17 (Black, 256 GB)"
        assert product.availability is Availability.IN_STOCK
        assert product.last_checked_at is not None
        assert product.last_success_at is not None
        assert product.consecutive_failures == 0

    def test_failed_check_records_a_row_and_counts_the_failure(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://shop.example.com/p/blocked"
        respx.get(url).mock(return_value=httpx.Response(403))
        product = service.add(url)

        execution = engine.check_product(db_session, product.id)

        assert execution.status is CheckStatus.FAILED
        assert execution.error_type == "blocked"
        assert execution.error_detail
        assert product.consecutive_failures == 1
        assert product.last_success_at is None

    def test_failed_check_does_not_claim_out_of_stock(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        """The rule that matters most, verified end to end through the database."""
        url = "https://shop.example.com/p/timeout"
        respx.get(url).mock(side_effect=httpx.ConnectTimeout("nope"))
        product = service.add(url)

        execution = engine.check_product(db_session, product.id)

        assert execution.availability_result is Availability.UNKNOWN
        assert product.availability is Availability.UNKNOWN
        assert product.availability is not Availability.OUT_OF_STOCK

    def test_price_not_found_is_partial_not_failed(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://www.flipkart.com/p/itmnoprice"
        respx.get(url).mock(
            return_value=httpx.Response(200, html=load("flipkart_no_price.html"))
        )
        product = service.add(url)

        execution = engine.check_product(db_session, product.id)

        assert execution.status is CheckStatus.PARTIAL
        assert execution.availability_result is Availability.UNKNOWN

    def test_a_configured_pincode_records_needs_location(
        self, service: ProductService, db_session: Session
    ) -> None:
        """End to end: the setting reaches the adapter and the reason reaches the row.

        Flipkart prices per delivery area and cannot be localised without a browser
        session, so with a PIN code configured the recorded reason says so instead of
        leaving "no price" to be read as a broken selector -- or, far worse, as stock
        information. The check is ``partial``: the request was fine, the answer was not
        available at this location.
        """
        settings = get_settings().model_copy(update={"delivery_pincode": "560037"})
        engine = TrackingEngine(StoreRegistry(), settings)
        url = "https://www.flipkart.com/p/itmpincode"
        route = respx.get(url).mock(
            return_value=httpx.Response(200, html=load("flipkart_no_price.html"))
        )
        product = service.add(url)

        execution = engine.check_product(db_session, product.id)

        assert execution.status is CheckStatus.PARTIAL
        assert execution.error_type == "needs_location"
        assert execution.extracted_price is None
        # The invariant. A location we could not set says nothing about stock.
        assert execution.availability_result is Availability.UNKNOWN
        assert product.availability is not Availability.OUT_OF_STOCK
        # Not transient: asking the same shop again immediately changes nothing, and
        # re-asking is what shops object to.
        assert route.call_count == 1

    def test_without_a_pincode_nothing_changes(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        """The feature stays invisible until somebody configures it."""
        url = "https://www.flipkart.com/p/itmnopin"
        respx.get(url).mock(
            return_value=httpx.Response(200, html=load("flipkart_no_price.html"))
        )
        product = service.add(url)

        execution = engine.check_product(db_session, product.id)

        assert execution.error_type == "price_not_found"

    def test_out_of_stock_is_a_successful_check(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://shop.example.com/p/oos"
        respx.get(url).mock(
            return_value=httpx.Response(200, html=load("jsonld_out_of_stock.html"))
        )
        product = service.add(url)

        execution = engine.check_product(db_session, product.id)

        assert execution.status is CheckStatus.SUCCESS
        assert execution.availability_result is Availability.OUT_OF_STOCK
        assert execution.extracted_price is None

    def test_a_failing_check_does_not_erase_known_fields(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        """A later failure must not wipe the name and price we already learned."""
        url = "https://shop.example.com/p/flaky"
        route = respx.get(url)
        route.mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        product = service.add(url)
        engine.check_product(db_session, product.id)

        route.mock(return_value=httpx.Response(503))
        engine.check_product(db_session, product.id)

        assert product.name == "Apple iPhone 17 (Black, 256 GB)"
        assert str(product.current_price) == "69999.00"

    def test_every_check_leaves_a_row(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://shop.example.com/p/repeat"
        respx.get(url).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )
        product = service.add(url)

        for _ in range(3):
            engine.check_product(db_session, product.id)

        assert len(CheckExecutionRepository(db_session).list_for_product(product.id)) == 3

    def test_check_records_duration(
        self, service: ProductService, engine: TrackingEngine, db_session: Session
    ) -> None:
        url = "https://shop.example.com/p/timing"
        respx.get(url).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )
        product = service.add(url)

        execution = engine.check_product(db_session, product.id)

        assert execution.duration_ms is not None
        assert execution.duration_ms >= 0

    def test_checking_a_missing_product_raises(
        self, engine: TrackingEngine, db_session: Session
    ) -> None:
        with pytest.raises(NotFoundError):
            engine.check_product(db_session, 999_999)
