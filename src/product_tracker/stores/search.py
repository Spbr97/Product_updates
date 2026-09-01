"""Finding candidate listings by searching a store, rather than being handed a URL.

The shape mirrors ``StoreAdapter``: an interface, a registry, and a configuration-driven
implementation that covers most stores without new Python. Adding search for a store is a
YAML file in ``searches/``; only a store whose results cannot be described that way needs
a class.

Three things this deliberately does *not* do:

* **It does not decide.** A search returns ranked candidates and the caller confirms.
  Auto-tracking the top hit is how you end up watching a Galaxy S25 FE at Rs 65,999 while
  believing you are watching the Galaxy S25 at Rs 79,999 -- the two differ by one word in
  the title, and that word is why :attr:`SearchHit.qualifiers` exists.
* **It does not work around a refusal.** A store that answers a search with a challenge or
  a 403 is reported BLOCKED, exactly as a product fetch would be.
* **It does not invent URLs.** Every hit is a link the store's own results page published.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote_plus, urljoin

import yaml
from bs4 import BeautifulSoup

from ..core.logging import get_logger
from ..domain.enums import FetchOutcome, SearchOutcome
from ..domain.errors import ConfigurationError
from ..domain.models import FetchContext, SearchHit, SearchResult
from ..utils.money import parse_price
from ..utils.urls import host_of
from . import browser as browser_module
from . import robots, sitemaps
from .http import FetchSuccess
from .http import fetch as http_fetch

log = get_logger(__name__)

SEARCH_DIR = Path(__file__).parent / "searches"

#: Words that mean "this is not the thing you asked for". A title carrying one that the
#: query did not is a *different product*, and the ranking says so rather than quietly
#: scoring it as a match.
#:
#: Two kinds, and both matter. Model qualifiers separate a Galaxy S25 from a Galaxy S25 FE.
#: Accessory words separate a phone from a case for that phone -- which matters most where
#: there is no relevance ranking to lean on: a store's sitemap lists "Samsung Galaxy S25
#: Silicone Case" and the phone itself as equally good answers to "Galaxy S25", and without
#: this the cases win on being listed first.
MODEL_QUALIFIERS: frozenset[str] = frozenset(
    {
        "fe", "pro", "max", "plus", "ultra", "mini", "air", "lite", "neo",
        "se", "prime", "power", "turbo", "active", "edge", "note", "refurbished",
        "renewed", "unlocked",
    }
)

#: Only unambiguous ones. "display", "unit", "glass" and "band" were here and had to go:
#: a phone's own title describes its display, so every real phone came back flagged as an
#: accessory. A word earns its place here only if a product title containing it is
#: essentially never the product itself.
ACCESSORY_WORDS: frozenset[str] = frozenset(
    {
        "case", "cases", "cover", "covers", "protector", "protectors",
        "film", "tempered", "charger", "cable", "adapter", "stand",
        "holder", "mount", "skin", "pouch", "sleeve", "strap", "dock",
        "grip", "stylus", "demo", "dummy", "spare",
    }
)

#: Everything that disqualifies a title from being an exact answer.
DISQUALIFYING_WORDS: frozenset[str] = MODEL_QUALIFIERS | ACCESSORY_WORDS

_TOKEN = re.compile(r"[a-z0-9]+")

#: How a store's results may be fetched.
_RENDER_MODES = frozenset({"http", "auto", "browser"})

#: How many outcomes map to which fetch failure, so a search reports the same distinctions
#: a product fetch does.
_FETCH_TO_SEARCH = {
    FetchOutcome.BLOCKED: SearchOutcome.BLOCKED,
    FetchOutcome.TIMEOUT: SearchOutcome.TIMEOUT,
    FetchOutcome.PAGE_STRUCTURE: SearchOutcome.PAGE_STRUCTURE,
}


def tokenise(text: str) -> list[str]:
    """Lowercase alphanumeric words. "iPhone 17 Pro" -> ['iphone', '17', 'pro']."""
    return _TOKEN.findall(text.lower())


def score_title(query: str, title: str) -> tuple[float, tuple[str, ...]]:
    """How well ``title`` answers ``query``, and which extra model words it carries.

    The score is the fraction of the query's words present in the title. The qualifiers are
    the words in the title that mean it is a different product -- a different model, or an
    accessory for the model asked about. They do not lower the score, they are reported
    alongside it, because "is an S25 FE close enough to an S25?" is a question for the
    person searching and not for a ranking function to answer silently.
    """
    wanted = tokenise(query)
    if not wanted:
        return 0.0, ()
    present = set(tokenise(title))

    matched = sum(1 for token in wanted if token in present)
    extra = tuple(
        sorted(token for token in present & DISQUALIFYING_WORDS if token not in set(wanted))
    )
    return matched / len(wanted), extra


class StoreSearch(ABC):
    """Finds candidate listings at one store."""

    slug: ClassVar[str]

    @abstractmethod
    def search(
        self,
        query: str,
        ctx: FetchContext,
        *,
        limit: int = 10,
        use_browser: bool = False,
    ) -> SearchResult:
        """Return ranked candidates. Never raises -- failures come back as an outcome.

        ``use_browser`` asks for the results page to be rendered. An implementation whose
        store needs no rendering may ignore it; one that cannot render should report
        :attr:`SearchOutcome.NEEDS_BROWSER` rather than quietly returning nothing.
        """


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """How to search one store, loaded from ``searches/<slug>.yaml``."""

    url_template: str
    #: CSS selector for the anchors that are product links on the results page.
    result_link: tuple[str, ...]
    #: Only hrefs matching this are treated as products, so navigation and ads are ignored.
    product_url_pattern: str
    title: tuple[str, ...] = ()
    #: Where the brand sits, when a store keeps it out of the title. Amazon does: the card
    #: holds "Apple" in one element and "iPhone Air 256 GB..." in another, so the title
    #: alone reads as "17 (12GB/512GB)" for a Xiaomi 17 -- which, in a tool whose job is
    #: identifying which product a listing is, is worse than useless.
    brand: tuple[str, ...] = ()
    price: tuple[str, ...] = ()
    image: tuple[str, ...] = ()
    #: The repeating block for one result. When set, parsing walks cards and looks inside
    #: each; without it, parsing scans anchors and reads the link's own text. Cards are
    #: much the better shape: a results page puts a product's title, price and link in one
    #: block, and "nearest ancestor div" reaches either too far (swallowing the neighbour's
    #: rating text) or not far enough (missing the price entirely).
    result_card: str | None = None
    #: "http" (default), "auto" (HTTP first, render when unreadable), or "browser"
    #: (render straight away, for shops verified to publish nothing without JavaScript).
    render: str = "http"
    #: Where this store's sitemap lives. "" means "ask robots.txt"; "none" disables it.
    sitemap: str = ""
    #: Regex picking the product children out of a sitemap index, so we do not pull the
    #: brand, category and blog sitemaps looking for products.
    sitemap_include: str = "product"
    sitemap_max_files: int = 20
    #: What to wait for once rendered. Worth setting whenever the results have a known
    #: shape: it returns as soon as they exist instead of sleeping a fixed interval.
    wait_for: str | None = None
    notes: str | None = None

    @property
    def may_render(self) -> bool:
        return self.render in _RENDER_MODES - {"http"}

    @property
    def has_sitemap(self) -> bool:
        return self.sitemap.strip().lower() != "none"

    def sitemap_spec(self) -> sitemaps.SitemapSpec:
        return sitemaps.SitemapSpec(
            index_url="" if self.sitemap.lower() in {"", "auto", "none"} else self.sitemap,
            include=self.sitemap_include,
            max_files=self.sitemap_max_files,
        )


def _as_tuple(value: object) -> tuple[str, ...]:
    """One selector or a list of them, always as a tuple."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return (str(value),)


#: Every key a search description may contain. Anything else is a mistake.
_KNOWN_KEYS = frozenset(
    {
        "url",
        "product_url_pattern",
        "result_card",
        "result_link",
        "render",
        "wait_for",
        "sitemap",
        "sitemap_include",
        "sitemap_max_files",
        "title",
        "brand",
        "price",
        "image",
        "notes",
    }
)


@cache
def load_search_config(slug: str) -> SearchConfig:
    path = SEARCH_DIR / f"{slug}.yaml"
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # A key that is not recognised is silently ignored by a plain .get(), and the result is
    # a search that runs, returns almost nothing, and blames the store. That happened here:
    # renaming "result_container" to "result_card" left two stores carrying the old key, so
    # they parsed the entire page as one result and could never return more than one hit.
    # Failing loudly at load time costs a startup error and saves that whole diagnosis.
    unknown = sorted(set(raw) - _KNOWN_KEYS)
    if unknown:
        raise ConfigurationError(
            f"{path.name} has unknown key(s): {', '.join(unknown)}. "
            f"Known keys are: {', '.join(sorted(_KNOWN_KEYS))}"
        )
    for required in ("url", "product_url_pattern"):
        if not raw.get(required):
            raise ConfigurationError(f"{path.name} is missing required key {required!r}")

    mode = str(raw.get("render", "http"))
    if mode not in _RENDER_MODES:
        raise ConfigurationError(
            f"{path.name} has render: {mode!r}; expected one of {', '.join(sorted(_RENDER_MODES))}"
        )

    return SearchConfig(
        url_template=str(raw["url"]),
        result_link=_as_tuple(raw.get("result_link")),
        product_url_pattern=str(raw["product_url_pattern"]),
        title=_as_tuple(raw.get("title")),
        brand=_as_tuple(raw.get("brand")),
        price=_as_tuple(raw.get("price")),
        image=_as_tuple(raw.get("image")),
        result_card=raw.get("result_card"),
        render=str(raw.get("render", "http")),
        sitemap=str(raw.get("sitemap", "")),
        sitemap_include=str(raw.get("sitemap_include", "product")),
        sitemap_max_files=int(raw.get("sitemap_max_files", 20)),
        wait_for=raw.get("wait_for"),
        notes=raw.get("notes"),
    )


class ConfiguredSearch(StoreSearch):
    """Search driven entirely by a YAML description of a store's results page."""

    def __init__(self, slug: str) -> None:
        self.slug = slug  # type: ignore[misc]

    @property
    def config(self) -> SearchConfig:
        return load_search_config(self.slug)

    def search(
        self,
        query: str,
        ctx: FetchContext,
        *,
        limit: int = 10,
        use_browser: bool = False,
    ) -> SearchResult:
        """Search this store, optionally rendering the results page.

        ``use_browser`` is the caller's decision, not this store's: whether rendering is
        *permitted* comes from the config's ``render`` mode, whether it is *worth it* comes
        from the caller, which knows whether the cheap pass already answered the question.
        """
        config = self.config
        url = config.url_template.format(query=quote_plus(query))

        # Asked before the request is made, not after it is refused. A site that publishes
        # "Disallow: /search?" has said no in the way sites are meant to be able to; going
        # ahead and reading the 403 as rate limiting is not a misunderstanding, it is not
        # listening.
        if not robots.is_allowed(url, ctx):
            return SearchResult.failure(
                self.slug,
                SearchOutcome.DISALLOWED,
                "this store's robots.txt asks crawlers not to fetch its search results, "
                "so we do not. Its product pages are still tracked normally.",
            )

        render = use_browser and config.may_render

        response = (
            browser_module.render(url, ctx, wait_for=config.wait_for)
            if render
            else http_fetch(url, ctx)
        )

        if render and browser_module.is_unavailable(response):
            return SearchResult.failure(
                self.slug,
                SearchOutcome.NEEDS_BROWSER,
                "this store publishes its results with JavaScript and no browser is "
                "installed. " + browser_module.UNAVAILABLE_MESSAGE,
            )

        if not isinstance(response, FetchSuccess):
            outcome = _FETCH_TO_SEARCH.get(response.outcome, SearchOutcome.ERROR)
            return SearchResult.failure(
                self.slug,
                outcome,
                response.message or "search request failed",
                http_status=response.http_status,
            )

        hits = self._parse(response, config, query, limit=limit)
        if not hits:
            # An empty results page and an unreadable one are different facts. A page that
            # published no product links at all is far more likely to have been rendered by
            # JavaScript than to mean the shop stocks nothing, so it is not reported as
            # "no results" unless the page looked like a results page.
            return SearchResult(
                store_slug=self.slug,
                outcome=SearchOutcome.PAGE_STRUCTURE,
                message=(
                    "the page carried no product links. The store may render its results "
                    "with JavaScript, may have served a reduced page because it is rate "
                    "limiting us, or the search selectors may need updating -- a 200 with "
                    "nothing in it does not say which"
                ),
                http_status=response.http_status,
            )

        log.info(
            "search.completed",
            store=self.slug,
            rendered=render,
            url_host=host_of(url),
            hits=len(hits),
            http_status=response.http_status,
        )
        return SearchResult(
            store_slug=self.slug,
            outcome=SearchOutcome.OK,
            hits=hits,
            http_status=response.http_status,
        )

    def _parse(
        self, response: FetchSuccess, config: SearchConfig, query: str, *, limit: int
    ) -> tuple[SearchHit, ...]:
        soup = BeautifulSoup(response.html, "html.parser")
        pattern = re.compile(config.product_url_pattern)

        seen: set[str] = set()
        hits: list[SearchHit] = []

        for card in self._cards(soup, config):
            url = self._link(card, config, pattern, response.url)
            if url is None:
                continue
            # One product appears several times in a card (image, title, rating); the
            # path without its query string identifies it.
            key = url.split("?")[0]
            if key in seen:
                continue

            title = self._full_title(card, config)
            if not title:
                continue
            seen.add(key)

            score, qualifiers = score_title(query, title)
            hits.append(
                SearchHit(
                    url=url,
                    title=title,
                    store_slug=self.slug,
                    price=self._price(card, config),
                    currency="INR",
                    score=score,
                    qualifiers=qualifiers,
                )
            )
            if len(hits) >= limit * 4:  # Enough to rank well without parsing the whole page.
                break

        hits.sort(key=lambda hit: (-hit.score, len(hit.qualifiers)))
        return tuple(hits[:limit])

    @staticmethod
    def _cards(soup: BeautifulSoup, config: SearchConfig) -> list[Any]:
        """The repeating result blocks, or the whole page when none is configured."""
        if config.result_card:
            return list(soup.select(config.result_card))
        return [soup]

    def _link(
        self, card: Any, config: SearchConfig, pattern: re.Pattern[str], base_url: str
    ) -> str | None:
        """The product URL for this result.

        The card itself is checked before its descendants. On Flipkart the result block *is*
        the product link -- anchoring on the href shape rather than a rotating class name --
        and ``select`` only ever looks at descendants, so a card that is its own link finds
        nothing and every result is skipped.
        """
        candidates = [card] if card.has_attr("href") else []
        for selector in config.result_link or ("a[href]",):
            candidates.extend(card.select(selector))

        for anchor in candidates:
            href = str(anchor.get("href") or "")
            if href and pattern.search(href):
                return urljoin(base_url, href)
        return None

    def _full_title(self, card: Any, config: SearchConfig) -> str | None:
        """The product's name, with its brand restored if the store keeps them apart."""
        title = self._text(card, config.title)
        if title is None:
            return None
        brand = self._text(card, config.brand)
        if not brand or title.casefold().startswith(brand.casefold()):
            return title
        return f"{brand} {title}"

    @staticmethod
    def _text(card: Any, selectors: tuple[str, ...]) -> str | None:
        for selector in selectors:
            for tag in card.select(selector):
                raw = str(tag.get("title") or tag.get("content") or "").strip()
                raw = raw or tag.get_text(" ", strip=True)
                if raw:
                    return " ".join(raw.split())
        return None

    def _price(self, card: Any, config: SearchConfig) -> Any:
        for selector in config.price:
            for tag in card.select(selector):
                price = parse_price(tag.get_text(" ", strip=True))
                if price is not None:
                    return price
        return None


@lru_cache(maxsize=1)
def available_searches() -> tuple[str, ...]:
    """Store slugs that have a search description."""
    if not SEARCH_DIR.is_dir():
        return ()
    return tuple(sorted(path.stem for path in SEARCH_DIR.glob("*.yaml")))


def search_for(slug: str) -> StoreSearch | None:
    """The search implementation for a store, or None when it has none configured."""
    if slug not in available_searches():
        return None
    return ConfiguredSearch(slug)


class SitemapSearch(StoreSearch):
    """Finds products in the sitemap a store publishes, rather than by searching it.

    This is the route for shops whose search we must not crawl (their robots.txt says so)
    or cannot read (their results are built by JavaScript). It asks a retailer for one
    static file instead of a rendered query, and after the first call it asks for nothing
    at all until the cache expires.

    The trade is that a sitemap carries URLs and nothing else. Titles here are derived from
    the URL slug and there are no prices, so every hit is marked ``from_sitemap``; the real
    title and price arrive with the first check of a tracked listing.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug  # type: ignore[misc]

    @property
    def config(self) -> SearchConfig:
        return load_search_config(self.slug)

    def search(
        self,
        query: str,
        ctx: FetchContext,
        *,
        limit: int = 10,
        use_browser: bool = False,
    ) -> SearchResult:
        config = self.config
        if not config.has_sitemap:
            return SearchResult.failure(
                self.slug, SearchOutcome.UNSUPPORTED, "no sitemap configured for this store"
            )

        base = config.url_template.format(query="x")
        urls = sitemaps.product_urls(
            self.slug, config.sitemap_spec(), config.product_url_pattern, base, ctx
        )
        if not urls:
            # Could not look, which is not the same as having looked and found nothing.
            return SearchResult.failure(
                self.slug,
                SearchOutcome.PAGE_STRUCTURE,
                "the store's sitemap could not be read, so its catalogue was not searched",
            )

        hits: list[SearchHit] = []
        for url in urls:
            words = sitemaps.slug_words(url)
            if not words:
                continue
            score, qualifiers = score_title(query, words)
            if score < 1.0:
                # Every word asked for must appear. A sitemap has no relevance ranking of
                # its own, so a partial match here is noise rather than a near miss.
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=sitemaps.title_from_slug(url),
                    store_slug=self.slug,
                    score=score,
                    qualifiers=qualifiers,
                    from_sitemap=True,
                )
            )

        hits.sort(key=lambda hit: (len(hit.qualifiers), len(hit.title)))
        log.info("search.sitemap", store=self.slug, catalogue=len(urls), hits=len(hits))
        if not hits:
            return SearchResult(
                store_slug=self.slug,
                outcome=SearchOutcome.NO_RESULTS,
                message=f"nothing matching in {len(urls)} catalogued products",
            )
        return SearchResult(
            store_slug=self.slug, outcome=SearchOutcome.OK, hits=tuple(hits[:limit])
        )
