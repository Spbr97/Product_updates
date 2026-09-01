"""Amazon India adapter.

Amazon product pages *are* served to us -- roughly two megabytes of real HTML over plain
HTTP. It is the homepage that answers with an AWS WAF JavaScript challenge, which is a
security control and is not something this adapter works around. If a product page ever
returns a challenge or a 403, the check is recorded as ``BLOCKED`` and left alone.

What the pages do not carry is JSON-LD, so everything comes from the selectors in
``selectors/amazon.yaml``. Two things about that are worth knowing, because both produced
real, confidently wrong answers before this adapter existed:

* **Price selectors must be anchored to the buy box.** A page-wide ``.a-price`` matches
  about fifteen "customers also bought" tiles, and the first of them priced a Rs 84,999
  phone at Rs 61,480.
* **Stock is stated in words**, not by the presence of a marker element. Amazon writes
  "In stock", "Currently unavailable", "Only 2 left in stock", and a dozen variants, so
  this reads the availability block's text and refuses to guess when it recognises none
  of them.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import ClassVar

from bs4 import BeautifulSoup

from ..core.logging import EVENT_FETCH_RESULT, get_logger
from ..domain.enums import Availability, FetchMethod, FetchOutcome
from ..domain.models import FetchContext, FetchResult
from ..utils.money import parse_price
from ..utils.urls import host_of
from . import browser as browser_module
from . import selector_config
from .base import DomainMatchAdapter
from .http import FetchSuccess, failure_to_result
from .http import fetch as http_fetch

log = get_logger(__name__)

CURRENCY = "INR"

#: Phrases meaning the item can be bought. Checked before the negative phrases because
#: "Only 2 left in stock" contains "in stock" and must not be read as a shortage warning.
_IN_STOCK_PHRASES: tuple[str, ...] = (
    "in stock",
    "usually dispatched",
    "usually ships",
    "available to ship",
)

#: Phrases meaning it cannot. Note "out of stock" is here and "in stock" above; the
#: negatives are tested first for exactly that reason.
_OUT_OF_STOCK_PHRASES: tuple[str, ...] = (
    "currently unavailable",
    "out of stock",
    "temporarily out of stock",
    "we don't know when or if this item will be back",
    "unavailable",
)


def read_availability(text: str | None) -> Availability:
    """Amazon's stock wording, turned into an availability.

    Returns UNKNOWN for anything unrecognised rather than assuming the item is buyable.
    Wording we have not seen tells us nothing, and inventing a verdict from it is precisely
    the failure this project refuses to make.
    """
    if not text:
        return Availability.UNKNOWN
    lowered = " ".join(text.lower().split())

    # Negatives first: "out of stock" contains "in stock" as a substring.
    for phrase in _OUT_OF_STOCK_PHRASES:
        if phrase in lowered:
            return Availability.OUT_OF_STOCK
    for phrase in _IN_STOCK_PHRASES:
        if phrase in lowered:
            return Availability.IN_STOCK
    return Availability.UNKNOWN


def read_price_to_pay(soup: BeautifulSoup) -> Decimal | None:
    """The price Amazon will actually charge.

    Amazon renders two prices in its buy box and labels them, which is the only reliable
    way to tell them apart:

    * ``priceToPay`` / ``apex-pricetopay-value`` -- what you pay.
    * ``a-text-price`` / ``apex-basisprice-value`` -- the struck-out MRP.

    The trap is that ``priceToPay``'s ``.a-offscreen`` is *empty*: the digits live in
    ``.a-price-whole`` and ``.a-price-fraction``. So a selector that reads ``.a-offscreen``
    inside the buy box finds nothing for the real price and falls through to the only
    non-empty one on the page -- the MRP. That is how this adapter came to report Rs 84,999
    for a phone Amazon was selling at Rs 61,480, and Rs 4,990 for Rs 799 earbuds. The
    number looked plausible every time, which is exactly what made it dangerous.
    """
    for node in soup.select("span.priceToPay, span.apex-pricetopay-value"):
        # The screen-reader copy when it has one, since it is unambiguous.
        offscreen = node.select_one(".a-offscreen")
        if offscreen is not None:
            price = parse_price(offscreen.get_text(strip=True))
            if price is not None:
                return price

        whole = node.select_one(".a-price-whole")
        if whole is None:
            continue
        fraction = node.select_one(".a-price-fraction")
        digits = whole.get_text(strip=True).rstrip(".,")
        if fraction is not None:
            digits = f"{digits}.{fraction.get_text(strip=True)}"
        price = parse_price(digits)
        if price is not None:
            return price
    return None


class AmazonAdapter(DomainMatchAdapter):
    slug: ClassVar[str] = "amazon-in"
    display_name: ClassVar[str] = "Amazon India"
    domains: ClassVar[tuple[str, ...]] = ("amazon.in",)

    @property
    def selectors(self) -> selector_config.SelectorSet:
        return selector_config.load_selectors("amazon")

    def _asin(self, url: str) -> str | None:
        pattern = self.selectors.extra.get("identifier_path_pattern")
        if not pattern:
            return None
        match = re.search(str(pattern), url)
        return match.group(1) if match else None

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
            # A block is a block. Re-asking through a browser is the beginning of evasion.
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
        soup = BeautifulSoup(response.html, "html.parser")

        identifier = self._asin(response.url) or self._asin(url)
        name = selector_config.select_text(soup, selectors.name)
        price = read_price_to_pay(soup) or selector_config.select_price(soup, selectors.price)
        image = selector_config.select_image(soup, selectors.image, response.url)

        availability = selector_config.select_availability(soup, selectors)
        if availability is Availability.UNKNOWN:
            wording = selector_config.select_text(
                soup, selector_config.as_tuple(selectors.extra.get("availability_text"))
            )
            availability = read_availability(wording)

        if availability is Availability.OUT_OF_STOCK:
            return FetchResult(
                outcome=FetchOutcome.OUT_OF_STOCK,
                availability=Availability.OUT_OF_STOCK,
                name=name,
                product_identifier=identifier,
                # The buy box is gone, so any price still on the page is not one that can
                # be paid. Reporting it would put an unbuyable number in price history.
                image_url=image,
                raw_metadata={"source": "selectors"},
                fetch_method=method,
                http_status=response.http_status,
                message="Amazon lists this item as unavailable",
            )

        if price is None:
            if name is None:
                return FetchResult.failure(
                    FetchOutcome.PAGE_STRUCTURE,
                    "page did not match any known Amazon product layout; the selectors "
                    "in selectors/amazon.yaml may need updating",
                    fetch_method=method,
                    http_status=response.http_status,
                )
            return FetchResult(
                outcome=FetchOutcome.PRICE_NOT_FOUND,
                # Identified the product, could not read a price. That says nothing about
                # stock, so whatever the availability block said stands on its own.
                availability=availability,
                name=name,
                product_identifier=identifier,
                image_url=image,
                raw_metadata={"source": "selectors"},
                fetch_method=method,
                http_status=response.http_status,
                message=(
                    "found the product but no price in the buy box; the listing may have "
                    "no direct offer, or the price selectors may need updating"
                ),
            )

        return FetchResult(
            outcome=FetchOutcome.OK,
            availability=availability,
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
