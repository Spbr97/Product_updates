"""Store adapters, driven by stubbed HTTP responses.

``respx`` intercepts httpx, so no request leaves the machine. The suite must never depend
on Flipkart or anyone else being reachable.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from product_tracker.domain.enums import Availability, CheckStatus, FetchMethod, FetchOutcome
from product_tracker.domain.models import FetchContext
from product_tracker.services.tracking import _status_for
from product_tracker.stores.flipkart import FlipkartAdapter
from product_tracker.stores.generic import GenericStoreAdapter
from product_tracker.stores.registry import StoreRegistry

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

#: Browser fallback off (these exercise the HTTP path, and Playwright is optional), and
#: host verification off: the stubbed hostnames deliberately do not resolve. The guard
#: itself is covered in test_urls.py and by the dedicated cases below.
CTX = FetchContext(timeout_seconds=5, allow_browser=False, verify_public_host=False)


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    """Activate respx for every test in this module.

    Not ``@respx.mock`` on the class: in respx 0.23 that decorator returns a *function*,
    so pytest silently stops collecting the class and the tests never run.
    """
    with respx.mock:
        yield


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def generic() -> GenericStoreAdapter:
    return GenericStoreAdapter()


@pytest.fixture
def flipkart() -> FlipkartAdapter:
    return FlipkartAdapter()


def stub(url: str, *, html: str = "", status: int = 200) -> None:
    respx.get(url).mock(
        return_value=httpx.Response(status, html=html) if html else httpx.Response(status)
    )


class TestRegistryResolution:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.flipkart.com/x/p/itm1", "flipkart"),
            ("https://flipkart.com/x/p/itm1", "flipkart"),
            ("https://dl.flipkart.com/s/abc", "flipkart"),
            ("https://example.com/p/1", "generic"),
            ("https://www.croma.com/p/123", "generic"),
        ],
    )
    def test_resolves_to_the_right_adapter(self, url: str, expected: str) -> None:
        assert StoreRegistry().resolve(url).slug == expected

    def test_lookalike_domain_does_not_match(self) -> None:
        """``notflipkart.com`` must not be claimed by the Flipkart adapter."""
        assert StoreRegistry().resolve("https://notflipkart.com/p/1").slug == "generic"

    def test_fallback_is_always_last(self) -> None:
        """Even if constructed fallback-first, a named adapter must still win."""
        registry = StoreRegistry([GenericStoreAdapter(), FlipkartAdapter()])
        assert registry.resolve("https://www.flipkart.com/x/p/itm1").slug == "flipkart"

    def test_lists_every_store(self) -> None:
        """Spelled out rather than derived, on purpose.

        Registering an adapter changes which sites get store-specific handling, so it
        should be a conscious edit here rather than something a test quietly absorbs.
        """
        slugs = {info.slug for info in StoreRegistry().list_stores()}
        assert slugs == {"amazon-in", "flipkart", "generic"}


class TestGenericAdapterSuccess:
    def test_reads_json_ld_product(self, generic: GenericStoreAdapter) -> None:
        stub("https://shop.test/p/1", html=load("jsonld_in_stock.html"))

        result = generic.fetch_product("https://shop.test/p/1", CTX)

        assert result.outcome is FetchOutcome.OK
        assert result.price == Decimal("69999.00")
        assert result.currency == "INR"
        assert result.availability is Availability.IN_STOCK
        assert result.fetch_method is FetchMethod.HTTP
        assert result.http_status == 200

    def test_reads_opengraph_product(self, generic: GenericStoreAdapter) -> None:
        stub("https://shop.test/p/2", html=load("opengraph_product.html"))

        result = generic.fetch_product("https://shop.test/p/2", CTX)

        assert result.outcome is FetchOutcome.OK
        assert result.price == Decimal("12499.00")

    def test_out_of_stock_is_a_success_with_no_price(
        self, generic: GenericStoreAdapter
    ) -> None:
        stub("https://shop.test/p/3", html=load("jsonld_out_of_stock.html"))

        result = generic.fetch_product("https://shop.test/p/3", CTX)

        assert result.outcome is FetchOutcome.OUT_OF_STOCK
        assert result.succeeded
        assert result.availability is Availability.OUT_OF_STOCK
        assert result.price is None

    def test_price_without_stated_availability_stays_unknown(
        self, generic: GenericStoreAdapter
    ) -> None:
        stub("https://shop.test/p/4", html=load("jsonld_no_availability.html"))

        result = generic.fetch_product("https://shop.test/p/4", CTX)

        assert result.price == Decimal("14999")
        assert result.availability is Availability.UNKNOWN


class TestGenericAdapterFailures:
    """The central rule: no failure may be reported as OUT_OF_STOCK."""

    def test_unparseable_page_is_page_structure(self, generic: GenericStoreAdapter) -> None:
        stub("https://shop.test/about", html=load("no_product.html"))

        result = generic.fetch_product("https://shop.test/about", CTX)

        assert result.outcome is FetchOutcome.PAGE_STRUCTURE
        assert result.availability is Availability.UNKNOWN

    @pytest.mark.parametrize(
        ("status", "outcome"),
        [(403, FetchOutcome.BLOCKED), (429, FetchOutcome.BLOCKED)],
    )
    def test_anti_bot_statuses_are_blocked(
        self, generic: GenericStoreAdapter, status: int, outcome: FetchOutcome
    ) -> None:
        stub("https://shop.test/p/9", status=status)

        result = generic.fetch_product("https://shop.test/p/9", CTX)

        assert result.outcome is outcome
        assert result.availability is Availability.UNKNOWN

    def test_captcha_body_is_blocked_even_with_http_200(
        self, generic: GenericStoreAdapter
    ) -> None:
        """A challenge page returns 200; only the body reveals it."""
        stub("https://shop.test/p/10", html=load("captcha_challenge.html"))

        result = generic.fetch_product("https://shop.test/p/10", CTX)

        assert result.outcome is FetchOutcome.BLOCKED
        assert result.availability is Availability.UNKNOWN

    @pytest.mark.parametrize("status", [404, 410])
    def test_missing_listing_is_unavailable(
        self, generic: GenericStoreAdapter, status: int
    ) -> None:
        """The listing is gone: a real finding, distinct from out of stock."""
        stub("https://shop.test/p/gone", status=status)

        result = generic.fetch_product("https://shop.test/p/gone", CTX)

        assert result.outcome is FetchOutcome.UNAVAILABLE
        assert result.availability is Availability.UNAVAILABLE
        assert result.availability is not Availability.OUT_OF_STOCK

    def test_server_error_is_transient(self, generic: GenericStoreAdapter) -> None:
        stub("https://shop.test/p/11", status=503)

        result = generic.fetch_product("https://shop.test/p/11", CTX)

        assert result.outcome is FetchOutcome.HTTP_ERROR
        assert result.outcome.is_transient
        assert result.availability is Availability.UNKNOWN

    def test_timeout_is_transient(self, generic: GenericStoreAdapter) -> None:
        respx.get("https://shop.test/p/12").mock(side_effect=httpx.ConnectTimeout("timed out"))

        result = generic.fetch_product("https://shop.test/p/12", CTX)

        assert result.outcome is FetchOutcome.TIMEOUT
        assert result.outcome.is_transient
        assert result.availability is Availability.UNKNOWN

    def test_connection_error_is_handled(self, generic: GenericStoreAdapter) -> None:
        respx.get("https://shop.test/p/13").mock(side_effect=httpx.ConnectError("refused"))

        result = generic.fetch_product("https://shop.test/p/13", CTX)

        assert result.outcome is FetchOutcome.HTTP_ERROR
        assert not result.succeeded

    def test_failure_messages_do_not_leak_the_url(self, generic: GenericStoreAdapter) -> None:
        """A URL can carry query-string secrets; keep it out of stored error text."""
        url = "https://shop.test/p/14?token=SUPERSECRET"
        respx.get(url).mock(side_effect=httpx.ConnectError("connection refused"))

        result = generic.fetch_product(url, CTX)

        assert "SUPERSECRET" not in (result.message or "")


class TestFlipkartAdapter:
    def test_reads_price_from_selectors(self, flipkart: FlipkartAdapter) -> None:
        url = "https://www.flipkart.com/apple-iphone-17/p/itm1?pid=MOBABC123"
        stub(url, html=load("flipkart_product.html"))

        result = flipkart.fetch_product(url, CTX)

        assert result.outcome is FetchOutcome.OK
        assert result.price == Decimal("69999")
        assert result.currency == "INR"
        assert result.name == "Apple iPhone 17 (Black, 256 GB)"
        assert result.availability is Availability.IN_STOCK

    def test_takes_identifier_from_the_pid_parameter(self, flipkart: FlipkartAdapter) -> None:
        url = "https://www.flipkart.com/apple-iphone-17/p/itm1?pid=MOBABC123"
        stub(url, html=load("flipkart_product.html"))

        assert flipkart.fetch_product(url, CTX).product_identifier == "MOBABC123"

    def test_sold_out_marker_is_respected(self, flipkart: FlipkartAdapter) -> None:
        url = "https://www.flipkart.com/apple-iphone-17/p/itm2"
        stub(url, html=load("flipkart_sold_out.html"))

        result = flipkart.fetch_product(url, CTX)

        assert result.outcome is FetchOutcome.OUT_OF_STOCK
        assert result.availability is Availability.OUT_OF_STOCK
        assert result.succeeded

    def test_missing_price_is_not_out_of_stock(self, flipkart: FlipkartAdapter) -> None:
        """Product found, price not. Stock is unknown -- Flipkart showed no sold-out marker."""
        url = "https://www.flipkart.com/apple-iphone-17/p/itm3"
        stub(url, html=load("flipkart_no_price.html"))

        result = flipkart.fetch_product(url, CTX)

        assert result.outcome is FetchOutcome.PRICE_NOT_FOUND
        assert result.availability is Availability.UNKNOWN
        assert result.availability is not Availability.OUT_OF_STOCK
        assert result.name == "Apple iPhone 17 (Blue, 256 GB)"

    def test_unrecognised_layout_reports_page_structure(
        self, flipkart: FlipkartAdapter
    ) -> None:
        url = "https://www.flipkart.com/whatever"
        stub(url, html="<html><body>redesigned</body></html>")

        result = flipkart.fetch_product(url, CTX)

        assert result.outcome is FetchOutcome.PAGE_STRUCTURE
        assert "selectors" in (result.message or "")

    def test_prefers_json_ld_when_present(self, flipkart: FlipkartAdapter) -> None:
        url = "https://www.flipkart.com/p/itm4"
        stub(url, html=load("jsonld_in_stock.html"))

        result = flipkart.fetch_product(url, CTX)

        assert result.raw_metadata["source"] == "json-ld"
        assert result.price == Decimal("69999.00")


class TestConvenienceAccessors:
    def test_get_price_and_availability(self, generic: GenericStoreAdapter) -> None:
        stub("https://shop.test/p/20", html=load("jsonld_in_stock.html"))

        assert generic.get_price("https://shop.test/p/20", CTX) == Decimal("69999.00")
        assert generic.get_availability("https://shop.test/p/20", CTX) is Availability.IN_STOCK

    def test_get_product_metadata(self, generic: GenericStoreAdapter) -> None:
        stub("https://shop.test/p/21", html=load("jsonld_in_stock.html"))

        metadata = generic.get_product_metadata("https://shop.test/p/21", CTX)

        assert metadata["name"] == "Apple iPhone 17 (Black, 256 GB)"
        assert metadata["product_identifier"] == "MOBHFN6YN2HXB5HE"


class TestStatusMapping:
    """How a fetch outcome becomes the status stored on the execution row."""

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (FetchOutcome.OK, CheckStatus.SUCCESS),
            (FetchOutcome.OUT_OF_STOCK, CheckStatus.SUCCESS),
            (FetchOutcome.UNAVAILABLE, CheckStatus.SUCCESS),
            (FetchOutcome.PRICE_NOT_FOUND, CheckStatus.PARTIAL),
            # Partial, not failed: nothing went wrong with the request. The shop simply
            # will not quote a price without a delivery area we cannot give it.
            (FetchOutcome.NEEDS_LOCATION, CheckStatus.PARTIAL),
            (FetchOutcome.BLOCKED, CheckStatus.FAILED),
            (FetchOutcome.TIMEOUT, CheckStatus.FAILED),
            (FetchOutcome.PAGE_STRUCTURE, CheckStatus.FAILED),
            (FetchOutcome.HTTP_ERROR, CheckStatus.FAILED),
            (FetchOutcome.ERROR, CheckStatus.FAILED),
        ],
    )
    def test_outcome_maps_to_status(
        self, outcome: FetchOutcome, expected: CheckStatus
    ) -> None:
        from product_tracker.domain.models import FetchResult

        result = (
            FetchResult(outcome=outcome, price=Decimal("1"), currency="INR")
            if outcome is FetchOutcome.OK
            else FetchResult(outcome=outcome)
        )
        assert _status_for(result) is expected


class TestBrowserFallback:
    """The generic and Flipkart adapters both fall back to rendering.

    ``stores.browser.render`` is stubbed: what matters here is *when* the adapters reach
    for it and what they do with the answer, not Chromium.
    """

    @pytest.fixture
    def allow_browser(self) -> FetchContext:
        return FetchContext(timeout_seconds=5, allow_browser=True, verify_public_host=False)

    def _stub_render(
        self, monkeypatch: pytest.MonkeyPatch, module: object, result: object
    ) -> list[str]:
        calls: list[str] = []

        def fake_render(url: str, _ctx: FetchContext, **_kwargs: object) -> object:
            calls.append(url)
            return result

        monkeypatch.setattr(module, "render", fake_render)
        return calls

    def test_generic_renders_when_http_finds_no_product(
        self,
        generic: GenericStoreAdapter,
        allow_browser: FetchContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from product_tracker.stores import browser as browser_module
        from product_tracker.stores.http import FetchSuccess

        stub("https://shop.test/js", html="<html><body>loading...</body></html>")
        calls = self._stub_render(
            monkeypatch,
            browser_module,
            FetchSuccess(
                html=load("jsonld_in_stock.html"), url="https://shop.test/js", http_status=200
            ),
        )

        result = generic.fetch_product("https://shop.test/js", allow_browser)

        assert calls == ["https://shop.test/js"]
        assert result.outcome is FetchOutcome.OK
        assert result.fetch_method is FetchMethod.BROWSER

    def test_generic_does_not_render_when_http_succeeds(
        self,
        generic: GenericStoreAdapter,
        allow_browser: FetchContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rendering costs an order of magnitude more; only reach for it when needed."""
        from product_tracker.stores import browser as browser_module
        from product_tracker.stores.http import FetchSuccess

        stub("https://shop.test/ok", html=load("jsonld_in_stock.html"))
        calls = self._stub_render(
            monkeypatch, browser_module, FetchSuccess(html="", url="", http_status=200)
        )

        generic.fetch_product("https://shop.test/ok", allow_browser)

        assert calls == []

    @pytest.mark.parametrize("status", [403, 404])
    def test_generic_does_not_render_a_block_or_a_missing_listing(
        self,
        generic: GenericStoreAdapter,
        allow_browser: FetchContext,
        monkeypatch: pytest.MonkeyPatch,
        status: int,
    ) -> None:
        """Both are real answers; re-fetching would neither change them nor be polite."""
        from product_tracker.stores import browser as browser_module
        from product_tracker.stores.http import FetchSuccess

        stub("https://shop.test/no", status=status)
        calls = self._stub_render(
            monkeypatch, browser_module, FetchSuccess(html="", url="", http_status=200)
        )

        generic.fetch_product("https://shop.test/no", allow_browser)

        assert calls == []

    def test_generic_reports_both_failures_when_rendering_also_fails(
        self,
        generic: GenericStoreAdapter,
        allow_browser: FetchContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from product_tracker.stores import browser as browser_module
        from product_tracker.stores.http import FetchFailure

        stub("https://shop.test/js2", status=503)
        self._stub_render(
            monkeypatch,
            browser_module,
            FetchFailure(FetchOutcome.ERROR, "chromium is not installed"),
        )

        result = generic.fetch_product("https://shop.test/js2", allow_browser)

        assert not result.succeeded
        assert "browser fallback also failed" in (result.message or "")
        assert "chromium is not installed" in (result.message or "")

    def test_flipkart_renders_when_selectors_find_no_price(
        self,
        flipkart: FlipkartAdapter,
        allow_browser: FetchContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from product_tracker.stores import browser as browser_module
        from product_tracker.stores.http import FetchSuccess

        url = "https://www.flipkart.com/p/itm-js"
        stub(url, html=load("flipkart_no_price.html"))
        calls = self._stub_render(
            monkeypatch,
            browser_module,
            FetchSuccess(html=load("flipkart_product.html"), url=url, http_status=200),
        )

        result = flipkart.fetch_product(url, allow_browser)

        assert calls == [url]
        assert result.outcome is FetchOutcome.OK
        assert result.price == Decimal("69999")

    def test_no_rendering_when_the_browser_is_disabled(
        self, generic: GenericStoreAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from product_tracker.stores import browser as browser_module
        from product_tracker.stores.http import FetchSuccess

        stub("https://shop.test/off", html="<html><body>nothing</body></html>")
        calls = self._stub_render(
            monkeypatch, browser_module, FetchSuccess(html="", url="", http_status=200)
        )

        result = generic.fetch_product("https://shop.test/off", CTX)

        assert calls == []
        assert result.outcome is FetchOutcome.PAGE_STRUCTURE


class TestDeliveryPincodeOnTheWire:
    """What actually leaves the machine when a delivery area is configured.

    ``apply`` returning the right dictionary is one thing; ``httpx`` sending it is
    another, and it is the wire that matters. These go through the real fetch path with
    respx intercepting, so the assertion is about the request that was made.
    """

    def test_nothing_extra_is_sent_when_no_pincode_is_configured(
        self, generic: GenericStoreAdapter
    ) -> None:
        url = "https://shop.example.com/p/plain"
        route = respx.get(url).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )

        generic.fetch_product(url, CTX)

        assert "cookie" not in route.calls[0].request.headers

    def test_a_cookie_rule_reaches_the_request(
        self, generic: GenericStoreAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No catalogued shop has such a rule today, so this installs one.

        It is the mechanism that is being tested: when a host is ever found that takes a
        bare PIN code in a cookie, one line in ``pincode.RULES`` must be enough.
        """
        from product_tracker.stores import pincode

        monkeypatch.setitem(
            pincode.RULES, "shop.example.com", pincode.PincodeRule(cookies=("pin",), note="x")
        )
        url = "https://shop.example.com/p/cookie"
        route = respx.get(url).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )

        generic.fetch_product(
            url, replace(CTX, delivery_pincode="560037")
        )

        assert "pin=560037" in route.calls[0].request.headers["cookie"]

    def test_a_query_rule_changes_the_url_fetched(
        self, generic: GenericStoreAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from product_tracker.stores import pincode

        monkeypatch.setitem(
            pincode.RULES, "shop.example.com", pincode.PincodeRule(query_param="pin", note="x")
        )
        route = respx.get("https://shop.example.com/p/query", params={"pin": "560037"}).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )

        result = generic.fetch_product(
            "https://shop.example.com/p/query", replace(CTX, delivery_pincode="560037")
        )

        assert route.called
        assert result.outcome is FetchOutcome.OK

    def test_an_unclassified_host_is_fetched_exactly_as_given(
        self, generic: GenericStoreAdapter
    ) -> None:
        """The whole feature must stay invisible for a host we know nothing about."""
        url = "https://shop.example.com/p/untouched?a=1"
        route = respx.get(url).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )

        generic.fetch_product(url, replace(CTX, delivery_pincode="560037"))

        assert str(route.calls[0].request.url) == url
        assert "cookie" not in route.calls[0].request.headers


class TestRegistryLookup:
    def test_get_by_slug(self) -> None:
        assert StoreRegistry().get("flipkart").slug == "flipkart"

    def test_unknown_slug_raises(self) -> None:
        from product_tracker.domain.errors import NoAdapterError

        with pytest.raises(NoAdapterError, match="no adapter registered"):
            StoreRegistry().get("does-not-exist")

    def test_a_url_with_no_host_has_no_adapter(self) -> None:
        """Even the fallback needs something to fetch."""
        from product_tracker.domain.errors import NoAdapterError

        with pytest.raises(NoAdapterError):
            StoreRegistry().resolve("not-a-url")

    def test_adapters_are_ordered_fallback_last(self) -> None:
        slugs = [adapter.slug for adapter in StoreRegistry().adapters]
        assert slugs[-1] == "generic"

    def test_store_info_round_trips(self) -> None:
        info = StoreRegistry().get("flipkart").store_info()
        assert info.slug == "flipkart"
        assert info.is_fallback is False
