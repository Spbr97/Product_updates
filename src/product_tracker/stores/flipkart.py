"""Flipkart adapter.

Flipkart product pages are largely client-rendered and use obfuscated, frequently-rotated
class names, so extraction goes JSON-LD first (occasionally present and by far the most
stable), then the CSS selectors in ``selectors/flipkart.yaml``.

Known limitation, stated plainly: Flipkart actively blocks automated access. A plain HTTP
fetch is often served an interstitial, and even a rendered page may be a challenge. Those
checks are recorded as ``BLOCKED`` and left alone -- no CAPTCHA solving, no evasion. A
direct product URL fetched through the browser fallback is the most reliable combination
available, and it is still not guaranteed.
"""

from __future__ import annotations

from typing import ClassVar

from bs4 import BeautifulSoup

from ..core.logging import EVENT_FETCH_RESULT, get_logger
from ..domain.enums import Availability, FetchMethod, FetchOutcome
from ..domain.models import FetchContext, FetchResult
from ..utils.urls import host_of
from . import browser as browser_module
from . import extraction, selector_config
from .base import DomainMatchAdapter
from .http import FetchSuccess, failure_to_result
from .http import fetch as http_fetch

log = get_logger(__name__)

CURRENCY = "INR"


class FlipkartAdapter(DomainMatchAdapter):
    slug: ClassVar[str] = "flipkart"
    display_name: ClassVar[str] = "Flipkart"
    domains: ClassVar[tuple[str, ...]] = ("flipkart.com", "dl.flipkart.com")

    @property
    def selectors(self) -> selector_config.SelectorSet:
        return selector_config.load_selectors(self.slug)

    def fetch_product(self, url: str, ctx: FetchContext) -> FetchResult:
        response = http_fetch(url, ctx)

        if isinstance(response, FetchSuccess):
            result = self._interpret(response, url, FetchMethod.HTTP)
            if result.succeeded or not ctx.allow_browser:
                self._log(url, result)
                return result
            http_result = result
        else:
            http_result = failure_to_result(response, FetchMethod.HTTP)
            if response.outcome in (FetchOutcome.BLOCKED, FetchOutcome.UNAVAILABLE):
                self._log(url, http_result)
                return http_result
            if not ctx.allow_browser:
                self._log(url, http_result)
                return http_result

        rendered = browser_module.render(url, ctx)
        if isinstance(rendered, FetchSuccess):
            result = self._interpret(rendered, url, FetchMethod.BROWSER)
            self._log(url, result)
            return result

        combined = FetchResult.failure(
            http_result.outcome,
            f"{http_result.message}; browser fallback also failed ({rendered.message})",
            fetch_method=FetchMethod.BROWSER,
            http_status=http_result.http_status,
        )
        self._log(url, combined)
        return combined

    def _interpret(self, response: FetchSuccess, url: str, method: FetchMethod) -> FetchResult:
        selectors = self.selectors
        identifier = selector_config.identifier_from_url(url, selectors.identifier_param)

        # JSON-LD when Flipkart provides it: richer and far more stable than class names.
        structured = extraction.from_json_ld(response.html, response.url)
        if structured is not None and structured.has_price:
            return FetchResult(
                outcome=FetchOutcome.OK,
                availability=structured.availability,
                name=structured.name,
                product_identifier=structured.identifier or identifier,
                price=structured.price,
                currency=structured.currency or CURRENCY,
                image_url=structured.image_url,
                raw_metadata={"source": "json-ld"},
                fetch_method=method,
                http_status=response.http_status,
            )

        soup = BeautifulSoup(response.html, "html.parser")
        name = selector_config.select_text(soup, selectors.name)
        price = selector_config.select_price(soup, selectors.price)
        marker_availability = selector_config.select_availability(soup, selectors)
        image = selector_config.select_image(soup, selectors.image, response.url)

        if marker_availability is Availability.OUT_OF_STOCK:
            return FetchResult(
                outcome=FetchOutcome.OUT_OF_STOCK,
                availability=Availability.OUT_OF_STOCK,
                name=name,
                product_identifier=identifier,
                image_url=image,
                raw_metadata={"source": "selectors"},
                fetch_method=method,
                http_status=response.http_status,
                message="Flipkart marked this listing as sold out",
            )

        if price is None:
            if name is None:
                return FetchResult.failure(
                    FetchOutcome.PAGE_STRUCTURE,
                    "page did not match any known Flipkart product layout; the selectors "
                    "in selectors/flipkart.yaml may need updating",
                    fetch_method=method,
                    http_status=response.http_status,
                )
            # Product identified, price not. Availability is unknown -- Flipkart shows no
            # sold-out marker either, and absence of a price is not absence of stock.
            return FetchResult(
                outcome=FetchOutcome.PRICE_NOT_FOUND,
                availability=Availability.UNKNOWN,
                name=name,
                product_identifier=identifier,
                image_url=image,
                raw_metadata={"source": "selectors"},
                fetch_method=method,
                http_status=response.http_status,
                message=(
                    "found the product but no price; Flipkart renders prices with "
                    "JavaScript, so enable PLAYWRIGHT_ENABLED, or the price selectors "
                    "may need updating"
                ),
            )

        # A price is present and no sold-out marker was found. Flipkart only renders the
        # buy box for purchasable items, so a price here is a genuine in-stock signal.
        return FetchResult(
            outcome=FetchOutcome.OK,
            availability=Availability.IN_STOCK,
            name=name,
            product_identifier=identifier,
            price=price,
            currency=CURRENCY,
            image_url=image,
            raw_metadata={"source": "selectors"},
            fetch_method=method,
            http_status=response.http_status,
        )

    def _log(self, url: str, result: FetchResult) -> None:
        log.info(
            EVENT_FETCH_RESULT,
            store=self.slug,
            url_host=host_of(url),
            outcome=result.outcome.value,
            availability=result.availability.value,
            price=str(result.price) if result.price is not None else None,
            method=result.fetch_method.value,
            http_status=result.http_status,
        )
