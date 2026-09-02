"""Finding products through the sitemaps retailers publish for crawlers.

This is the sanctioned route, and for several shops it is the only one. A sitemap exists to
be read: it is advertised in robots.txt, it is a list of the URLs the site wants indexed,
and reading it asks far less of a retailer than rendering their search page in Chromium
once per query.

It also works where searching does not:

* **Flipkart, Reliance Digital and BigBasket disallow crawling their search** in robots.txt.
  Their sitemaps are published anyway, so discovery is still possible without ignoring what
  they asked for.
* **Vijay Sales and Sangeetha serve search results only to JavaScript**, and not even to a
  real browser in a way we could read. Their sitemaps are static XML.

What a sitemap does not carry is a price or a title -- only URLs, and sometimes a
last-modified date. So a hit from here has a name *derived from the URL slug*, which is why
:attr:`SearchHit.from_sitemap` exists to say so. The real title and price arrive on the
first check, from the product page itself.

Sizes are real and are bounded on purpose: Vijay Sales publishes 5,928 products in one
1.6 MB file; Reliance splits roughly eight thousand across sixteen. Fetching a retailer's
entire catalogue on every search would be worse behaviour than the search scraping this
replaces, so results are cached on disk and the number of files fetched is capped.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..core.logging import get_logger
from ..domain.models import FetchContext
from .http import FetchSuccess
from .http import fetch as http_fetch

log = get_logger(__name__)

#: How long a downloaded sitemap is reused. A day: catalogues change slowly, and the point
#: of the cache is that a search costs nothing after the first one.
CACHE_TTL_SECONDS = 24 * 3600.0

#: Guard rails. A retailer's whole catalogue is not something to pull on a whim.
MAX_CHILD_SITEMAPS = 20
MAX_URLS = 200_000
MAX_BYTES = 40_000_000

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_IS_INDEX = re.compile(r"<sitemapindex", re.IGNORECASE)
_SITEMAP_DIRECTIVE = re.compile(r"(?im)^\s*sitemap:\s*(\S+)")


@dataclass(frozen=True, slots=True)
class SitemapSpec:
    """Where a store's product URLs live."""

    #: The index or urlset to start from. Empty means "ask robots.txt".
    index_url: str = ""
    #: Which children of an index are worth fetching, as a regex on the child URL. Without
    #: it we would pull brand, category and blog sitemaps to find products.
    include: str = "product"
    max_files: int = MAX_CHILD_SITEMAPS


def cache_dir() -> Path:
    """Where downloaded sitemaps live between runs."""
    import tempfile

    directory = Path(tempfile.gettempdir()) / "product-tracker-sitemaps"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _cache_path(slug: str) -> Path:
    return cache_dir() / f"{slug}.json"


def _read_cache(slug: str) -> list[str] | None:
    path = _cache_path(slug)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - float(payload.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
        return None
    urls = payload.get("urls")
    return list(urls) if isinstance(urls, list) else None


def _write_cache(slug: str, urls: list[str]) -> None:
    """Write the catalogue, atomically.

    Temp file then ``os.replace``, which is atomic on both platforms. A plain write is not:
    two processes refreshing the same catalogue at once can interleave, and a reader then
    finds truncated JSON. It degrades to a refetch rather than corrupting anything, but a
    refetch is a megabyte of somebody else's bandwidth.
    """
    path = _cache_path(slug)
    payload = json.dumps({"fetched_at": time.time(), "urls": urls})
    try:
        # In the same directory, so the replace is a rename rather than a copy across
        # filesystems. Unique per process so two writers do not share the temp file either.
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        # A cache that cannot be written is a slow search, not a broken one.
        log.debug("sitemap.cache_write_failed", store=slug, detail=str(error)[:80])


def clear_cache() -> None:
    """Forget every cached sitemap."""
    for path in cache_dir().glob("*.json"):
        path.unlink(missing_ok=True)


def _fetch_xml(url: str, ctx: FetchContext) -> str | None:
    """Fetch one sitemap, decompressing it when it is served as .gz."""
    response = http_fetch(url, ctx)
    if not isinstance(response, FetchSuccess):
        log.debug(
            "sitemap.fetch_failed",
            url_host=urlsplit(url).netloc,
            outcome=str(response.outcome),
        )
        return None

    text = response.html
    if url.endswith(".gz") and not text.lstrip().startswith("<"):
        # ``.xml.gz`` arrives as bytes the HTTP layer decoded as text; recover and inflate.
        try:
            return gzip.decompress(text.encode("latin-1", errors="ignore")).decode(
                "utf-8", errors="replace"
            )
        except (OSError, ValueError, UnicodeDecodeError):
            log.debug("sitemap.gunzip_failed", url_host=urlsplit(url).netloc)
            return None
    return text


def discover_indexes(base_url: str, ctx: FetchContext) -> tuple[str, ...]:
    """Every sitemap a site advertises in its robots.txt.

    All of them, not the first. Vijay Sales and Reliance advertise a single index, but
    Flipkart lists a dozen roots side by side -- browse sitemaps first, product indexes
    further down -- so taking the first would quietly search the wrong file and conclude
    the catalogue held nothing.
    """
    parts = urlsplit(base_url)
    robots_url = f"{parts.scheme or 'https'}://{parts.netloc}/robots.txt"
    response = http_fetch(robots_url, ctx)
    if not isinstance(response, FetchSuccess):
        return ()
    return tuple(_SITEMAP_DIRECTIVE.findall(response.html))


def product_urls(
    slug: str, spec: SitemapSpec, product_pattern: str, base_url: str, ctx: FetchContext
) -> tuple[str, ...]:
    """Every product URL this store publishes, cached on disk.

    Returns an empty tuple when the sitemap cannot be read -- which the caller must report
    as "we could not look", never as "the store has no such product".
    """
    cached = _read_cache(slug)
    if cached is not None:
        return tuple(cached)

    roots = (spec.index_url,) if spec.index_url else discover_indexes(base_url, ctx)
    if spec.include and not spec.index_url:
        # Narrow the advertised roots to the ones that look like products, so a site
        # listing a dozen sitemaps costs one fetch rather than twelve.
        include = re.compile(spec.include, re.IGNORECASE)
        narrowed = tuple(r for r in roots if include.search(r))
        roots = narrowed or roots
    if not roots:
        log.debug("sitemap.none_advertised", store=slug)
        return ()

    collected: list[str] = []
    for root in roots[: min(spec.max_files, MAX_CHILD_SITEMAPS)]:
        collected.extend(_collect(slug, root, spec, product_pattern, ctx))
        if len(collected) >= MAX_URLS:
            break

    urls = tuple(dict.fromkeys(collected))[:MAX_URLS]
    _write_cache(slug, list(urls))
    return urls


def _collect(
    slug: str, index_url: str, spec: SitemapSpec, product_pattern: str, ctx: FetchContext
) -> tuple[str, ...]:
    body = _fetch_xml(index_url, ctx)
    if body is None:
        return ()

    pattern = re.compile(product_pattern)
    include = re.compile(spec.include, re.IGNORECASE) if spec.include else None

    if not _IS_INDEX.search(body):
        # A flat urlset: the products are right here.
        return tuple(u for u in _LOC.findall(body) if pattern.search(u))[:MAX_URLS]

    children = _LOC.findall(body)
    if include is not None:
        children = [c for c in children if include.search(c)]
    children = children[: min(spec.max_files, MAX_CHILD_SITEMAPS)]

    collected: list[str] = []
    budget = MAX_BYTES
    for child in children:
        child_body = _fetch_xml(child, ctx)
        if child_body is None:
            continue
        budget -= len(child_body)
        collected.extend(u for u in _LOC.findall(child_body) if pattern.search(u))
        if len(collected) >= MAX_URLS or budget <= 0:
            log.info("sitemap.budget_reached", store=slug, urls=len(collected))
            break

    log.info("sitemap.collected", store=slug, files=len(children), urls=len(collected))
    return tuple(collected[:MAX_URLS])


#: Path segments that are furniture rather than description. Samsung ends every product
#: page with ``/buy/``, so without this the readable part of
#: ``/in/smartphones/galaxy-s25/buy/`` is the word "buy" -- and every Samsung product
#: scores zero against every query. No real product slug is one of these words.
_NON_DESCRIPTIVE_SEGMENTS: frozenset[str] = frozenset(
    {"buy", "buys", "product", "products", "item", "items", "detail", "details", "index"}
)


def slug_words(url: str) -> str:
    """The readable part of a product URL, as words.

    ``/p/P237290/237287/samsung-galaxy-s25-5g-12gb-ram-256gb-storage-icyblue`` becomes
    ``samsung galaxy s25 5g 12gb ram 256gb storage icyblue``. Retailers put the model,
    capacity and colour in the slug, which is exactly what a query is trying to match.
    """
    path = urlsplit(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ""
    # The last segment that is not purely an id, and not path furniture, carries the
    # description.
    for segment in reversed(segments):
        if segment.isdigit() or segment.lower() in _NON_DESCRIPTIVE_SEGMENTS:
            continue
        if re.search(r"[a-z]{3}", segment, re.IGNORECASE):
            return re.sub(r"[-_+]+", " ", segment).strip()
    return re.sub(r"[-_+]+", " ", segments[-1]).strip()


def title_from_slug(url: str) -> str:
    """A readable name derived from the URL.

    Derived, not published: it is what the retailer put in their URL, not the title they
    show on the page. The real one arrives with the first check. Capacities are upper-cased
    because "256Gb" reads as a typo where "256GB" reads as a size.
    """
    words = slug_words(url).split()
    rendered = []
    for word in words:
        # "256Gb" reads as a typo; "256GB" reads as a size. Same for model codes.
        if re.fullmatch(r"\d+(gb|tb|mb|w|k|l)|[a-z]\d+|\d+[a-z]", word, re.IGNORECASE):
            rendered.append(word.upper())
        else:
            rendered.append(word.capitalize())
    return " ".join(rendered)


def fingerprint(urls: tuple[str, ...]) -> str:
    """A short digest of a collection, for logging without dumping thousands of URLs."""
    digest = hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()
    return digest[:12]
