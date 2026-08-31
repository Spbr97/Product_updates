import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .config import Product, Retailer
from .models import Offer

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36", "Accept-Language": "en-IN,en;q=0.9"}

def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from _walk(child)
    elif isinstance(value, list):
        for child in value: yield from _walk(child)

def _price(value) -> Decimal | None:
    if value is None: return None
    digits = re.sub(r"[^0-9.]", "", str(value).replace(",", ""))
    try: return Decimal(digits) if digits else None
    except InvalidOperation: return None

def _match(title: str, product: Product) -> bool:
    normalized = title.lower()
    return all(term.lower() in normalized for term in product.required_terms) and not any(term.lower() in normalized for term in product.excluded_terms)

def _products_from_html(html: str, source_url: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select('script[type="application/ld+json"]'):
        try: yield from _walk(json.loads(tag.get_text(strip=True)))
        except (json.JSONDecodeError, TypeError): continue
    # Fallback for product pages that publish OpenGraph / schema meta tags.
    title = soup.select_one('meta[property="og:title"]')
    price = soup.select_one('meta[property="product:price:amount"], meta[itemprop="price"]')
    if title and price:
        yield {"@type": "Product", "name": title.get("content", ""), "offers": {"price": price.get("content"), "availability": "InStock"}}
        return
    # Some retailers render a human-readable product price but omit JSON-LD.
    # Restrict this fallback to labelled prices to avoid picking a specification.
    heading = soup.find("h1")
    visible_price = re.search(r"(?:selling\s+)?price\s*:\s*(?:₹|rs\.?\s*)?([0-9][0-9,]+)", soup.get_text(" ", strip=True), re.IGNORECASE)
    if heading and visible_price:
        yield {"@type": "Product", "name": heading.get_text(" ", strip=True), "offers": {"price": visible_price.group(1), "availability": "InStock"}}

def _to_offer(node: dict, retailer: str, source_url: str) -> Offer | None:
    kind = node.get("@type", "")
    if isinstance(kind, list): kind = " ".join(kind)
    if "product" not in str(kind).lower(): return None
    offers = node.get("offers") or node.get("Offer") or {}
    if isinstance(offers, list): offers = offers[0] if offers else {}
    title = str(node.get("name") or "")
    price = _price(offers.get("price") or offers.get("lowPrice"))
    if not title or price is None: return None
    availability = str(offers.get("availability", "InStock")).lower()
    url = urljoin(source_url, str(offers.get("url") or node.get("url") or source_url))
    return Offer(retailer=retailer, title=title, url=url, price=price, available="outofstock" not in availability and "soldout" not in availability, observed_at=datetime.now(timezone.utc))

def _browser_fetch(url: str, timeout: int) -> tuple[str, str]:
    """Render a public page; never supplies credentials or defeats access controls."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed; run: pip install -e '.[browser,dev]' then playwright install chromium") from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="en-IN")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(1500)
            return page.content(), page.url
        finally:
            browser.close()

def scan_url(url: str, retailer: str, product: Product, timeout: int, browser_fallback: bool) -> tuple[list[Offer], str | None]:
    try:
        response = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        html, resolved_url, http_issue = response.text, str(response.url), None
    except httpx.HTTPError as exc:
        html, resolved_url, http_issue = "", url, str(exc)
    seen, offers = set(), []
    for node in _products_from_html(html, resolved_url):
        offer = _to_offer(node, retailer, resolved_url)
        if offer and _match(offer.title, product) and offer.key not in seen:
            seen.add(offer.key); offers.append(offer)
    if not offers and browser_fallback:
        try:
            html, resolved_url = _browser_fetch(url, timeout)
            for node in _products_from_html(html, resolved_url):
                offer = _to_offer(node, retailer, resolved_url)
                if offer and _match(offer.title, product) and offer.key not in seen:
                    seen.add(offer.key); offers.append(offer)
        except Exception as exc:
            return [], f"{retailer}: HTTP read failed ({http_issue or 'no matching price'}); browser fallback unavailable/blocked ({exc})"
    if offers: return offers, None
    detail = "browser-rendered page has no readable matching price" if browser_fallback else "no readable matching price"
    return [], f"{retailer}: {detail}; a PIN selection, sign-in, CAPTCHA, or direct product URL may be required"

def scan(retailers: list[Retailer], listing_urls: list[tuple[str, str]], product: Product, timeout: int, browser_fallback: bool = True) -> tuple[list[Offer], list[str]]:
    all_offers, issues = [], []
    for retailer in retailers:
        offers, issue = scan_url(str(retailer.search_url), retailer.name, product, timeout, browser_fallback)
        all_offers.extend(offers)
        if issue: issues.append(issue)
    for retailer, url in listing_urls:
        offers, issue = scan_url(url, retailer, product, timeout, browser_fallback)
        all_offers.extend(offers)
        if issue: issues.append(issue)
    return all_offers, issues
