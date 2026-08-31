"""Structured-data extraction shared by adapters.

Reads, in order of trustworthiness:

1. **schema.org JSON-LD** (``<script type="application/ld+json">``) -- the richest and
   most stable source, published by most large retailers.
2. **OpenGraph / microdata meta tags** -- less detail, but widely present.

Availability is the delicate part. ``parse_availability`` returns ``UNKNOWN`` whenever the
page does not state stock explicitly. That includes the common case of an offer that has a
price but no ``availability`` field: a price is not evidence of stock, and inventing
``IN_STOCK`` there would produce false "back in stock" alerts. Being honestly unsure is a
supported state; guessing is not.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..domain.enums import Availability
from ..utils.money import parse_currency, parse_price

#: schema.org ItemAvailability values mapped onto our vocabulary.
#:
#: PreOrder / BackOrder are deliberately UNKNOWN: the item is orderable but not in stock,
#: and calling it either would be wrong.
_AVAILABILITY_MAP: dict[str, Availability] = {
    "instock": Availability.IN_STOCK,
    "instoreonly": Availability.IN_STOCK,
    "onlineonly": Availability.IN_STOCK,
    "limitedavailability": Availability.IN_STOCK,
    "outofstock": Availability.OUT_OF_STOCK,
    "soldout": Availability.OUT_OF_STOCK,
    "discontinued": Availability.UNAVAILABLE,
    "preorder": Availability.UNKNOWN,
    "presale": Availability.UNKNOWN,
    "backorder": Availability.UNKNOWN,
}

_PRICE_META = (
    'meta[property="product:price:amount"]',
    'meta[property="og:price:amount"]',
    'meta[itemprop="price"]',
    'meta[name="twitter:data1"]',
)
_CURRENCY_META = (
    'meta[property="product:price:currency"]',
    'meta[property="og:price:currency"]',
    'meta[itemprop="priceCurrency"]',
)

#: A labelled price in visible text ("Price: Rs. 69,999"). Requiring the label avoids
#: picking up an EMI figure or a specification number.
_LABELLED_PRICE = re.compile(
    r"(?:selling\s+|sale\s+|offer\s+|our\s+)?price\s*[:\-]?\s*"
    r"(?:₹|rs\.?\s*|inr\s*)?([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)


class ProductData:
    """Fields extracted from a page. Any of them may be absent."""

    __slots__ = ("availability", "currency", "identifier", "image_url", "name", "price", "raw")

    def __init__(self) -> None:
        self.name: str | None = None
        self.price: Decimal | None = None
        self.currency: str | None = None
        self.availability: Availability = Availability.UNKNOWN
        self.image_url: str | None = None
        self.identifier: str | None = None
        self.raw: dict[str, Any] = {}

    @property
    def has_price(self) -> bool:
        return self.price is not None

    def __repr__(self) -> str:
        return (
            f"ProductData(name={self.name!r}, price={self.price!r}, "
            f"currency={self.currency!r}, availability={self.availability})"
        )


def parse_availability(raw: object) -> Availability:
    """Map a schema.org availability value onto our vocabulary.

    Anything absent, empty, or unrecognised is ``UNKNOWN`` -- never a guess.
    """
    if raw is None:
        return Availability.UNKNOWN
    text = str(raw).strip().lower()
    if not text:
        return Availability.UNKNOWN
    # Values are usually URLs: http://schema.org/InStock
    token = text.rsplit("/", 1)[-1].rsplit("#", 1)[-1].replace("_", "").replace("-", "")
    return _AVAILABILITY_MAP.get(token, Availability.UNKNOWN)


def iter_json_objects(value: object) -> Iterator[dict[str, Any]]:
    """Walk arbitrarily nested JSON, yielding every object.

    JSON-LD nests products inside ``@graph``, ``mainEntity``, ``itemListElement`` and
    similar, so a recursive walk is more reliable than knowing every wrapper.
    """
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def _is_product_node(node: dict[str, Any]) -> bool:
    kind = node.get("@type", "")
    if isinstance(kind, list):
        kind = " ".join(str(k) for k in kind)
    return "product" in str(kind).lower()


def _first_offer(node: dict[str, Any]) -> dict[str, Any]:
    offers = node.get("offers") or node.get("Offer") or {}
    if isinstance(offers, list):
        for candidate in offers:
            if isinstance(candidate, dict):
                return candidate
        return {}
    return offers if isinstance(offers, dict) else {}


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _as_text(value.get("name") or value.get("@id") or value.get("url"))
    if isinstance(value, list):
        for item in value:
            text = _as_text(item)
            if text:
                return text
        return None
    return str(value).strip() or None


def from_json_ld(html: str, base_url: str) -> ProductData | None:
    """Extract from the first JSON-LD Product node that yields a usable price."""
    soup = BeautifulSoup(html, "html.parser")
    best: ProductData | None = None

    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(tag.get_text(strip=True) or "null")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        for node in iter_json_objects(payload):
            if not _is_product_node(node):
                continue
            data = _product_from_node(node, base_url)
            if data is None:
                continue
            if data.has_price:
                return data
            # Keep a price-less product as a fallback: name and availability are still
            # worth reporting, and an explicit OutOfStock offer legitimately has no price.
            best = best or data

    return best


def _product_from_node(node: dict[str, Any], base_url: str) -> ProductData | None:
    name = _as_text(node.get("name"))
    offer = _first_offer(node)

    data = ProductData()
    data.name = name
    data.price = parse_price(offer.get("price") or offer.get("lowPrice"))
    data.currency = parse_currency(
        offer.get("priceCurrency"), offer.get("price"), default=None
    )
    data.availability = parse_availability(offer.get("availability"))
    data.identifier = _as_text(
        node.get("sku") or node.get("mpn") or node.get("productID") or node.get("gtin13")
    )

    image = node.get("image")
    image_text = _as_text(image.get("url") if isinstance(image, dict) else image)
    data.image_url = urljoin(base_url, image_text) if image_text else None

    data.raw = {"source": "json-ld"}
    if data.name is None and not data.has_price:
        return None
    return data


def from_meta_tags(html: str, base_url: str) -> ProductData | None:
    """Extract from OpenGraph / microdata meta tags."""
    soup = BeautifulSoup(html, "html.parser")

    price_tag = _first_meta(soup, _PRICE_META)
    title_tag = soup.select_one('meta[property="og:title"], meta[name="title"]')
    if price_tag is None and title_tag is None:
        return None

    data = ProductData()
    data.name = _content(title_tag) or _heading_text(soup)
    data.price = parse_price(_content(price_tag))
    data.currency = parse_currency(_content(_first_meta(soup, _CURRENCY_META)), default=None)

    availability_tag = soup.select_one(
        'meta[property="product:availability"], meta[property="og:availability"], '
        'link[itemprop="availability"], meta[itemprop="availability"]'
    )
    raw_availability = (
        _content(availability_tag)
        or (availability_tag.get("href") if availability_tag else None)
        if availability_tag
        else None
    )
    data.availability = parse_availability(raw_availability)

    image_tag = soup.select_one('meta[property="og:image"], meta[itemprop="image"]')
    image = _content(image_tag)
    data.image_url = urljoin(base_url, image) if image else None

    data.raw = {"source": "meta-tags"}
    if data.name is None and not data.has_price:
        return None
    return data


def from_labelled_text(html: str, base_url: str) -> ProductData | None:
    """Last resort: a heading plus a price that is explicitly labelled as one.

    Only labelled prices are accepted. Grabbing the first currency-looking number on a
    page reliably picks up EMI instalments, delivery charges, or "customers also bought"
    tiles.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    if heading is None:
        return None

    match = _LABELLED_PRICE.search(soup.get_text(" ", strip=True))
    if match is None:
        return None

    price = parse_price(match.group(1))
    if price is None:
        return None

    data = ProductData()
    data.name = heading.get_text(" ", strip=True) or None
    data.price = price
    data.currency = None
    data.availability = Availability.UNKNOWN
    data.raw = {"source": "labelled-text"}
    return data


STRATEGIES = (from_json_ld, from_meta_tags, from_labelled_text)


def extract(html: str, base_url: str) -> ProductData | None:
    """Run every strategy and return the most informative result.

    Preference order, strongest evidence first: a price, then an explicit stock statement,
    then merely having identified the product. Strategies are ordered by trustworthiness,
    so ties are broken in favour of JSON-LD.
    """
    results = [data for data in (s(html, base_url) for s in STRATEGIES) if data is not None]
    if not results:
        return None

    for predicate in (
        lambda d: d.has_price,
        lambda d: d.availability is not Availability.UNKNOWN,
        lambda d: bool(d.name),
    ):
        for data in results:
            if predicate(data):
                return data
    return None


def _first_meta(soup: BeautifulSoup, selectors: tuple[str, ...]) -> Any:
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag is not None and _content(tag):
            return tag
    return None


def _content(tag: Any) -> str | None:
    if tag is None:
        return None
    value = tag.get("content")
    return str(value).strip() if value else None


def _heading_text(soup: BeautifulSoup) -> str | None:
    heading = soup.find("h1")
    return heading.get_text(" ", strip=True) or None if heading else None
