"""The store adapter interface.

Every site integration implements :class:`StoreAdapter`. The tracking engine only ever
holds this type, so it has no knowledge of any particular site -- that is what lets a new
store ship without touching the engine.

Contract for implementers:

* ``fetch_product`` **must not raise** for an ordinary site problem. Return a
  :class:`~product_tracker.domain.models.FetchResult` whose ``outcome`` says what went
  wrong. Exceptions are reserved for genuine bugs.
* ``availability`` is a separate finding from ``outcome``. If you could not read the page,
  or read it but found no price, availability is ``UNKNOWN``. Only set ``OUT_OF_STOCK``
  when the page actually said so.
* Never work around a CAPTCHA, a login wall, or any other access control. Return
  ``FetchOutcome.BLOCKED`` and let the check be recorded as failed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, ClassVar

from ..domain.enums import Availability
from ..domain.models import FetchContext, FetchResult, StoreInfo
from ..utils.urls import host_of


class StoreAdapter(ABC):
    """Extracts price and availability for one family of URLs."""

    #: Stable identifier, matching a row in the ``stores`` table.
    slug: ClassVar[str]
    #: Human-readable name.
    display_name: ClassVar[str]
    #: Hostnames this adapter claims. Empty for a fallback adapter.
    domains: ClassVar[tuple[str, ...]] = ()
    #: True for the last-resort adapter that accepts anything.
    is_fallback: ClassVar[bool] = False

    @abstractmethod
    def can_handle_url(self, url: str) -> bool:
        """Whether this adapter recognises the URL."""

    @abstractmethod
    def fetch_product(self, url: str, ctx: FetchContext) -> FetchResult:
        """Fetch the page and extract what it can. Does not raise for site problems."""

    # --- Conveniences -------------------------------------------------------------
    # Each performs a full fetch and projects one field. They exist because the brief
    # names them; the tracking engine calls ``fetch_product`` once and reads the whole
    # result, because fetching three times to answer three questions would be rude to
    # the store and inconsistent for us.

    def get_price(self, url: str, ctx: FetchContext) -> Decimal | None:
        return self.fetch_product(url, ctx).price

    def get_availability(self, url: str, ctx: FetchContext) -> Availability:
        return self.fetch_product(url, ctx).availability

    def get_product_metadata(self, url: str, ctx: FetchContext) -> dict[str, Any]:
        result = self.fetch_product(url, ctx)
        return {
            "name": result.name,
            "product_identifier": result.product_identifier,
            "image_url": result.image_url,
            "currency": result.currency,
            **result.raw_metadata,
        }

    def store_info(self) -> StoreInfo:
        return StoreInfo(
            slug=self.slug,
            display_name=self.display_name,
            domains=self.domains,
            adapter_key=self.slug,
            is_fallback=self.is_fallback,
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} slug={self.slug!r}>"


class DomainMatchAdapter(StoreAdapter, ABC):
    """Base for adapters that claim URLs by hostname.

    Matches the host exactly or as a subdomain, so ``flipkart.com`` also claims
    ``www.flipkart.com`` but never ``notflipkart.com``.
    """

    def can_handle_url(self, url: str) -> bool:
        host = host_of(url)
        if not host:
            return False
        return any(host == domain or host.endswith(f".{domain}") for domain in self.domains)
