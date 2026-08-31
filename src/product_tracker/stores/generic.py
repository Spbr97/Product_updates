"""Fallback adapter for any site publishing structured product data.

Accepts any http(s) URL and reads schema.org JSON-LD, OpenGraph tags, or a labelled price.
That covers a surprising share of retailers, so an unrecognised site is still trackable
without writing an adapter for it.

The tradeoff is honesty about limits: sites that render prices purely in JavaScript need
the browser fallback, and sites that publish nothing structured return
``PRICE_NOT_FOUND`` with a message explaining what to try.
"""

from __future__ import annotations

from typing import ClassVar

from ..core.logging import EVENT_FETCH_RESULT, get_logger
from ..domain.enums import Availability, FetchMethod, FetchOutcome
from ..domain.models import FetchContext, FetchResult
from ..utils.money import DEFAULT_CURRENCY
from ..utils.urls import host_of
from . import browser as browser_module
from . import extraction
from .base import StoreAdapter
from .http import FetchSuccess, failure_to_result
from .http import fetch as http_fetch

log = get_logger(__name__)


class GenericStoreAdapter(StoreAdapter):
    """Structured-data adapter used when no named adapter claims the URL."""

    slug: ClassVar[str] = "generic"
    display_name: ClassVar[str] = "Generic (schema.org)"
    domains: ClassVar[tuple[str, ...]] = ()
    is_fallback: ClassVar[bool] = True

    #: Currency assumed when a page states a price but no currency. Only applied when the
    #: price itself carried no symbol to infer from.
    default_currency: ClassVar[str] = DEFAULT_CURRENCY

    def can_handle_url(self, url: str) -> bool:
        """Accept anything with a host -- this is the last resort in the registry."""
        return bool(host_of(url))

    def fetch_product(self, url: str, ctx: FetchContext) -> FetchResult:
        response = http_fetch(url, ctx)

        if isinstance(response, FetchSuccess):
            result = self._interpret(response, FetchMethod.HTTP)
            if result.succeeded or not ctx.allow_browser:
                self._log(url, result)
                return result
            # HTTP gave us a page but no usable data; it may be client-rendered.
            http_result = result
        else:
            http_result = failure_to_result(response, FetchMethod.HTTP)
            # A block or a missing listing is a real answer. Re-fetching in a browser
            # would neither change it nor be polite.
            if response.outcome in (FetchOutcome.BLOCKED, FetchOutcome.UNAVAILABLE):
                self._log(url, http_result)
                return http_result
            if not ctx.allow_browser:
                self._log(url, http_result)
                return http_result

        rendered = browser_module.render(url, ctx)
        if isinstance(rendered, FetchSuccess):
            result = self._interpret(rendered, FetchMethod.BROWSER)
            if result.succeeded:
                self._log(url, result)
                return result
            self._log(url, result)
            return result

        # Browser also failed: report the HTTP attempt, which is the more informative of
        # the two, but mention that rendering was tried.
        combined = FetchResult.failure(
            http_result.outcome,
            f"{http_result.message}; browser fallback also failed ({rendered.message})",
            fetch_method=FetchMethod.BROWSER,
            http_status=http_result.http_status,
        )
        self._log(url, combined)
        return combined

    def _interpret(self, response: FetchSuccess, method: FetchMethod) -> FetchResult:
        data = extraction.extract(response.html, response.url)

        if data is None:
            return FetchResult.failure(
                FetchOutcome.PAGE_STRUCTURE,
                "page contains no recognisable product data (no schema.org JSON-LD, "
                "OpenGraph product tags, or labelled price)",
                fetch_method=method,
                http_status=response.http_status,
            )

        if data.price is None:
            # No price. Availability may still be a genuine finding -- an out-of-stock
            # listing legitimately has no price.
            if data.availability in (Availability.OUT_OF_STOCK, Availability.UNAVAILABLE):
                outcome = (
                    FetchOutcome.OUT_OF_STOCK
                    if data.availability is Availability.OUT_OF_STOCK
                    else FetchOutcome.UNAVAILABLE
                )
                return FetchResult(
                    outcome=outcome,
                    availability=data.availability,
                    name=data.name,
                    product_identifier=data.identifier,
                    image_url=data.image_url,
                    raw_metadata=data.raw,
                    fetch_method=method,
                    http_status=response.http_status,
                    message="listing has no price because it is not purchasable",
                )
            # Price genuinely missing. Availability stays whatever the page said, which
            # for most such pages is UNKNOWN -- and must not become OUT_OF_STOCK.
            return FetchResult(
                outcome=FetchOutcome.PRICE_NOT_FOUND,
                availability=data.availability,
                name=data.name,
                product_identifier=data.identifier,
                image_url=data.image_url,
                raw_metadata=data.raw,
                fetch_method=method,
                http_status=response.http_status,
                message=(
                    "found the product but no price; the page may require a PIN code or "
                    "render its price with JavaScript"
                ),
            )

        return FetchResult(
            outcome=FetchOutcome.OK,
            availability=data.availability,
            name=data.name,
            product_identifier=data.identifier,
            price=data.price,
            currency=data.currency or self.default_currency,
            image_url=data.image_url,
            raw_metadata=data.raw,
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
            currency=result.currency,
            method=result.fetch_method.value,
            http_status=result.http_status,
        )

