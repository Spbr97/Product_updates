"""Selector configuration loading and selector-driven extraction.

Site-specific CSS lives in ``selectors/<slug>.yaml``, never in Python. When a retailer
changes its markup -- which they do often -- the fix is a data edit, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from functools import cache
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import yaml
from bs4 import BeautifulSoup

from ..domain.enums import Availability
from ..utils.money import parse_price

#: Directory of YAML files inside the installed package.
_PACKAGE = "product_tracker.stores"
_DIRECTORY = "selectors"


@dataclass(frozen=True, slots=True)
class SelectorSet:
    """Selectors for one store, loaded from YAML."""

    name: tuple[str, ...] = ()
    price: tuple[str, ...] = ()
    out_of_stock: tuple[str, ...] = ()
    image: tuple[str, ...] = ()
    identifier_param: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@cache
def load_selectors(slug: str) -> SelectorSet:
    """Load and cache ``selectors/<slug>.yaml``.

    Raises FileNotFoundError if an adapter names a config that was not packaged -- better
    a loud failure at startup than silently extracting nothing forever.
    """
    path = resources.files(_PACKAGE).joinpath(_DIRECTORY, f"{slug}.yaml")
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}

    known = {"name", "price", "out_of_stock", "image", "identifier_param"}
    return SelectorSet(
        name=as_tuple(raw.get("name")),
        price=as_tuple(raw.get("price")),
        out_of_stock=as_tuple(raw.get("out_of_stock")),
        image=as_tuple(raw.get("image")),
        identifier_param=raw.get("identifier_param"),
        extra={k: v for k, v in raw.items() if k not in known},
    )


def as_tuple(value: object) -> tuple[str, ...]:
    """Accept a single selector or a list of them, so YAML can use either form."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    raise TypeError(f"expected a string or list of strings, got {type(value).__name__}")


def select_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str | None:
    """First non-empty text (or ``content`` attribute) among the selectors.

    Every element a selector matches is tried, not just the first. Retailers routinely put
    an empty node ahead of the real one -- a screen-reader placeholder, a mobile variant
    hidden by CSS -- and stopping at the first match reads that emptiness as "no match" and
    moves on to a weaker selector.
    """
    for selector in selectors:
        for tag in soup.select(selector):
            content = tag.get("content")
            text = str(content).strip() if content else tag.get_text(" ", strip=True)
            if text:
                return text
    return None


def select_price(soup: BeautifulSoup, selectors: tuple[str, ...]) -> Decimal | None:
    """First parseable price among the selectors.

    As with :func:`select_text`, every match is tried. Amazon's core price block leads with
    an empty ``.a-offscreen`` node and carries the real price in the next one, so taking
    only the first match found nothing and fell through to a selector that matched a
    "customers also bought" tile instead -- a confidently wrong price for another product.
    """
    for selector in selectors:
        for tag in soup.select(selector):
            price = parse_price(tag.get_text(" ", strip=True))
            if price is not None:
                return price
    return None


def select_image(soup: BeautifulSoup, selectors: tuple[str, ...], base_url: str) -> str | None:
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag is None:
            continue
        source = tag.get("src") or tag.get("content") or tag.get("data-src")
        if source:
            return urljoin(base_url, str(source).strip())
    return None


def select_availability(soup: BeautifulSoup, selectors: SelectorSet) -> Availability:
    """Availability from markers alone.

    Only ``OUT_OF_STOCK`` can be concluded here, because only that has an explicit marker.
    Absence of the marker is not evidence of stock -- the marker may simply have been
    renamed -- so the caller decides what a price without a marker means.
    """
    for selector in selectors.out_of_stock:
        if soup.select_one(selector) is not None:
            return Availability.OUT_OF_STOCK
    return Availability.UNKNOWN


def identifier_from_url(url: str, param: str | None) -> str | None:
    """Pull the product identifier out of a query parameter (Flipkart's ``pid``)."""
    if not param:
        return None
    values = parse_qs(urlsplit(url).query).get(param)
    return values[0] if values else None
