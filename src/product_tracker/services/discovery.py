"""Finding a product across stores, so nobody has to paste five URLs.

Fans a query out over every store that has a search description, ranks what comes back, and
hands the caller candidates. It stops there on purpose.

The temptation is to take the best hit per store and start tracking it. That is precisely
how a watchlist ends up holding a Galaxy S25 FE at Rs 65,999 next to a Galaxy S25 at
Rs 79,999 and calling the FE the better deal: the titles differ by one word, both match a
search for "Galaxy S25", and no ranking function can know which one was meant. So the
candidates come back with their score and their qualifiers, and a person -- or an explicit
``--auto`` -- decides.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial

from ..core.config import Settings
from ..core.logging import get_logger
from ..domain.enums import SearchOutcome
from ..domain.models import CheckGuard, FetchContext, SearchHit, SearchResult
from ..scheduler.throttle import build_guard
from ..stores import browser, robots
from ..stores.catalogue import KNOWN_STORES
from ..stores.registry import StoreRegistry, default_registry
from ..stores.search import (
    BrowseSearch,
    SearchConfig,
    SitemapSearch,
    available_searches,
    load_search_config,
    search_for,
)
from ..utils.urls import host_of
from .query_policy import require_specific
from .specs import detect_category

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Discovery:
    """What searching every store turned up for one query."""

    query: str
    results: tuple[SearchResult, ...] = ()

    @property
    def hits(self) -> tuple[SearchHit, ...]:
        """Every candidate, best first."""
        found = [hit for result in self.results for hit in result.hits]
        found.sort(key=lambda hit: (-hit.score, len(hit.qualifiers), hit.store_slug))
        return tuple(found)

    @property
    def exact(self) -> tuple[SearchHit, ...]:
        """Candidates matching every word asked for, with no extra model qualifier."""
        return tuple(hit for hit in self.hits if hit.is_exact)

    def best_per_store(self, *, exact_only: bool = True) -> dict[str, SearchHit]:
        """The strongest candidate at each store.

        ``exact_only`` is the default because a near-match is usually a different product,
        and silently tracking one is the failure this module exists to avoid.
        """
        source = self.exact if exact_only else self.hits
        best: dict[str, SearchHit] = {}
        for hit in source:
            best.setdefault(hit.store_slug, hit)
        return best

    @property
    def unsearchable(self) -> tuple[SearchResult, ...]:
        """Stores that could not answer, and why. Never silently dropped."""
        return tuple(result for result in self.results if not result.succeeded)


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    query: str
    store_slugs: tuple[str, ...] = ()
    limit_per_store: int = 8
    #: Stores with no search description at all, reported so the gap is visible.
    skipped: tuple[str, ...] = field(default_factory=tuple)


def searchable_stores() -> tuple[str, ...]:
    """Catalogued stores that have a search description, in catalogue order."""
    configured = set(available_searches())
    return tuple(
        store.slug for store in KNOWN_STORES if not store.is_fallback and store.slug in configured
    )


def unsearchable_stores() -> tuple[str, ...]:
    """Catalogued stores with no search description yet."""
    configured = set(available_searches())
    return tuple(
        store.slug
        for store in KNOWN_STORES
        if not store.is_fallback and store.slug not in configured
    )


def discover(
    query: str,
    settings: Settings,
    *,
    store_slugs: tuple[str, ...] | None = None,
    limit_per_store: int = 8,
    allow_browser: bool = True,
    guard: CheckGuard | None = None,
) -> Discovery:
    """Search every configured store for ``query``.

    Two passes. The first fetches every store over plain HTTP, which answers most queries
    in a couple of seconds. The second renders -- in a *single* shared browser -- only the
    stores that came back unreadable, and only when the cheap pass produced no exact match.

    The trigger is "no exact match" rather than "no hits at all" deliberately. A search that
    returned only near-misses -- a Galaxy S25 FE when a Galaxy S25 was asked for -- has not
    actually answered the question, and the shops that might hold the real thing are exactly
    the ones worth the extra cost.

    A store that fails contributes a failed result rather than an exception, so one blocked
    retailer never costs the others their answers -- the same contract product checks hold
    themselves to.
    """
    # Before a single request leaves the machine. A query that names a category rather
    # than a product cannot be answered by any of these routes -- see ``query_policy`` --
    # and fanning it out to every shop would spend their patience to return noise.
    require_specific(query)

    targets = store_slugs if store_slugs is not None else searchable_stores()
    # Paced by default, not only when a caller remembers to ask. Search fans out across
    # every shop and can run several passes, and an unthrottled fan-out is how this tool
    # got one retailer to stop answering it altogether: the guard existed, `discover` took
    # one, and nothing ever passed it in.
    guard = guard or _default_guard(settings)
    ctx = FetchContext(
        timeout_seconds=settings.http_timeout_seconds,
        # Each pass decides for itself; the adapters never escalate on their own here.
        allow_browser=False,
        user_agent=settings.http_user_agent,
    )

    results = {
        slug: _guarded(
            slug, guard, partial(_search_one, slug, query, ctx, limit_per_store)
        )
        for slug in targets
    }
    discovery = Discovery(query=query, results=tuple(results.values()))

    if allow_browser and not discovery.exact:
        retry = _worth_rendering(results)
        if retry:
            results.update(
                _render_pass(retry, query, ctx, limit_per_store, settings, guard)
            )
            discovery = Discovery(query=query, results=tuple(results.values()))

    # Last, the catalogues. This runs whenever a store failed to answer -- *not* only when
    # nothing was found anywhere, which is the gate the render pass uses.
    #
    # The difference matters and it took seeing the output to notice. This is a price
    # comparison tool: "Amazon has it" is not the answer, "here is what each shop charges"
    # is. Gating on "no exact match anywhere" meant the first shop to answer suppressed
    # every other shop, and a comparison across six retailers quietly became a quote from
    # one. The cost is affordable precisely here: a catalogue is one static file the shop
    # publishes for crawlers, cached for a day, and never their search.
    catalogues = _worth_cataloguing(results)
    if catalogues:
        results.update(_sitemap_pass(catalogues, query, ctx, limit_per_store, guard))
        discovery = Discovery(query=query, results=tuple(results.values()))

    # Then the browse listings, for a shop that has neither a searchable results page nor
    # a walkable catalogue. Flipkart is the whole reason this pass exists: its search is
    # disallowed and its product sitemap is 275 million URLs, but the category and brand
    # listings it publishes for crawlers carry titles and prices.
    browsable = _worth_browsing(results)
    if browsable:
        results.update(_browse_pass(browsable, query, ctx, limit_per_store, guard))
        discovery = Discovery(query=query, results=tuple(results.values()))

    # Finally, prices. A catalogue publishes URLs and nothing else, so up to this point
    # every hit from a shop whose search we may not crawl has come back priceless -- and a
    # price comparison whose rows say "(no price)" for four shops out of six has not
    # compared anything. This fetches the product pages of the best few, which is the same
    # request a tracked listing's check makes.
    discovery = _with_prices(discovery, ctx, settings, guard)

    log.info(
        "discovery.completed",
        query_length=len(query),
        stores=len(results),
        hits=len(discovery.hits),
        exact=len(discovery.exact),
    )
    return discovery


def _with_prices(
    discovery: Discovery, ctx: FetchContext, settings: Settings, guard: CheckGuard | None
) -> Discovery:
    """Fill in prices for the best hits that arrived without one.

    Bounded by ``search_price_lookups`` and taken in rank order, because the point is to
    make the *answer* comparable, not to price a whole catalogue. Each lookup is paced by
    the same guard as everything else, and asks robots.txt first: these URLs came from a
    catalogue we chose to read, not from a person handing us a link.

    A lookup that fails changes nothing. The hit keeps its derived title and its absent
    price, and stays marked ``from_sitemap`` -- an unreadable page is not evidence about
    what a shop charges, and must not be recorded as one.
    """
    budget = settings.search_price_lookups
    if budget <= 0:
        return discovery

    wanted = [hit for hit in discovery.hits if hit.price is None][:budget]
    if not wanted:
        return discovery

    registry = default_registry()
    priced: dict[str, SearchHit] = {}
    for hit in wanted:
        if not robots.is_allowed(hit.url, ctx):
            continue
        outcome = _guarded(
            hit.store_slug, guard, partial(_price_one, registry, hit, ctx)
        )
        if outcome.hits:
            priced[hit.url] = outcome.hits[0]

    if not priced:
        return discovery

    log.info("discovery.priced", looked_up=len(wanted), found=len(priced))
    return Discovery(
        query=discovery.query,
        results=tuple(
            replace(result, hits=tuple(priced.get(hit.url, hit) for hit in result.hits))
            for result in discovery.results
        ),
    )


def _worth_browsing(results: dict[str, SearchResult]) -> tuple[str, ...]:
    """Stores still unanswered that publish browse listings.

    Last of the three routes because it is the most expensive: a listing page is large,
    ordered by the shop rather than by our question, and may need paging. It runs only
    where the cheaper routes have already failed.
    """
    candidates: list[str] = []
    for slug, result in results.items():
        if result.succeeded:
            continue
        config = _render_mode(slug)
        if config is not None and config.has_browse:
            candidates.append(slug)
    return tuple(candidates)


def _browse_pass(
    slugs: tuple[str, ...],
    query: str,
    ctx: FetchContext,
    limit: int,
    guard: CheckGuard | None = None,
) -> dict[str, SearchResult]:
    """Read each store's own category listing for the product.

    The category is worked out here rather than in the stores layer, which must not reach
    up into services to do it. A shop files a phone and a power bank in different places,
    and picking the wrong one is how a query for a Samsung phone matches their monitors.
    """
    category = detect_category(query)
    log.info("discovery.browse_pass", stores=len(slugs), category=category)
    found: dict[str, SearchResult] = {}
    for slug in slugs:
        result = _guarded(
            slug,
            guard,
            partial(BrowseSearch(slug, category).search, query, ctx, limit=limit),
        )
        if result.succeeded:
            found[slug] = result
    return found


def _price_one(registry: StoreRegistry, hit: SearchHit, ctx: FetchContext) -> SearchResult:
    """One product-page fetch, expressed as a SearchResult so the guard can pace it.

    The title is replaced too when the page publishes one. A catalogue title is derived
    from the URL slug; the page's own title is what the shop actually calls the product,
    so ``from_sitemap`` is cleared only when a real title arrives with the price.
    """
    fetched = registry.resolve(hit.url).fetch_product(hit.url, ctx)
    price = getattr(fetched, "price", None)
    if price is None:
        return SearchResult.failure(
            hit.store_slug, SearchOutcome.PAGE_STRUCTURE, "no price on the product page"
        )

    published = (getattr(fetched, "name", None) or "").strip()
    return SearchResult(
        store_slug=hit.store_slug,
        outcome=SearchOutcome.OK,
        hits=(
            replace(
                hit,
                price=price,
                currency=getattr(fetched, "currency", None) or hit.currency or "INR",
                title=published or hit.title,
                from_sitemap=not published,
            ),
        ),
    )


#: Outcomes that say something about us rather than about the shop.
_NOT_THE_HOSTS_FAULT = frozenset(
    {
        SearchOutcome.DISALLOWED,
        SearchOutcome.UNSUPPORTED,
        SearchOutcome.NEEDS_BROWSER,
    }
)


def _default_guard(settings: Settings) -> CheckGuard:
    """The same per-host pacing and circuit breaking a scheduled check gets.

    Shared across processes, which is what search actually needs: two people searching at
    once are two processes, and two in-memory guards would each believe they were alone.
    """
    return build_guard(settings)


def _guarded(
    slug: str, guard: CheckGuard | None, call: Callable[[], SearchResult]
) -> SearchResult:
    """Run one store's lookup behind the throttle, recording how it went."""
    if guard is None:
        return call()

    host = _host_for(slug)
    decision = guard.before(host)
    if not decision.proceed:
        return SearchResult.failure(
            slug, SearchOutcome.ERROR, decision.reason or "throttled"
        )
    result = call()
    # Only the host's own behaviour counts towards its circuit breaker. A search we
    # declined to make (their robots.txt said not to), or could not make (no browser
    # installed), is our decision and not a failure on their part -- and counting it would
    # open the circuit against a host we are still checking *products* on quite happily.
    if result.outcome not in _NOT_THE_HOSTS_FAULT:
        guard.after(host, succeeded=result.succeeded)
    return result


def _search_one(
    slug: str, query: str, ctx: FetchContext, limit: int, *, use_browser: bool = False
) -> SearchResult:
    search = search_for(slug)
    if search is None:
        return SearchResult.failure(
            slug, SearchOutcome.UNSUPPORTED, "no search is configured for this store"
        )
    return search.search(query, ctx, limit=limit, use_browser=use_browser)


def _worth_rendering(results: dict[str, SearchResult]) -> tuple[str, ...]:
    """Stores whose HTTP answer might improve if the page were rendered.

    Only PAGE_STRUCTURE qualifies. A store that refused us will refuse a rendered request
    too, and re-asking through a browser is the first step towards working around a
    refusal -- which this project does not do.
    """
    retry: list[str] = []
    for slug, result in results.items():
        if result.outcome is not SearchOutcome.PAGE_STRUCTURE:
            continue
        config = _render_mode(slug)
        if config is not None and config.may_render:
            retry.append(slug)
    return tuple(retry)


def _worth_cataloguing(results: dict[str, SearchResult]) -> tuple[str, ...]:
    """Stores whose sitemap might answer what their search could not.

    Includes DISALLOWED, which is the important case: a shop that asks us not to crawl its
    search still publishes a catalogue for crawlers to read, so respecting the first does
    not mean giving up on the second.
    """
    wanted = {
        SearchOutcome.DISALLOWED,
        SearchOutcome.PAGE_STRUCTURE,
        SearchOutcome.BLOCKED,
        SearchOutcome.NEEDS_BROWSER,
        SearchOutcome.NO_RESULTS,
    }
    candidates: list[str] = []
    for slug, result in results.items():
        if result.outcome not in wanted:
            continue
        config = _render_mode(slug)
        if config is not None and config.has_sitemap:
            candidates.append(slug)
    return tuple(candidates)


def _sitemap_pass(
    slugs: tuple[str, ...],
    query: str,
    ctx: FetchContext,
    limit: int,
    guard: CheckGuard | None = None,
) -> dict[str, SearchResult]:
    """Look each store up in its own published catalogue."""
    log.info("discovery.sitemap_pass", stores=len(slugs))
    found: dict[str, SearchResult] = {}
    for slug in slugs:
        result = _guarded(
            slug, guard, partial(SitemapSearch(slug).search, query, ctx, limit=limit)
        )
        # Only replace the earlier answer when this one is better. A store that asked us
        # not to crawl its search should keep saying so if its catalogue helps no more.
        if result.succeeded:
            found[slug] = result
    return found


def _render_mode(slug: str) -> SearchConfig | None:
    try:
        return load_search_config(slug)
    except Exception:  # A broken config is reported by the search itself, not here.
        return None


def _render_pass(
    slugs: tuple[str, ...],
    query: str,
    ctx: FetchContext,
    limit: int,
    settings: Settings,
    guard: CheckGuard | None,
) -> dict[str, SearchResult]:
    """Render the given stores, all inside one browser.

    Sequential, not concurrent. Rendering is heavier on a retailer than a fetch, and the
    shared browser has already removed the cost that concurrency would be buying back.
    """
    rendered: dict[str, SearchResult] = {}
    log.info("discovery.render_pass", stores=len(slugs))

    with browser.session(headless=settings.playwright_headless):
        for slug in slugs:
            result = _guarded(
                slug,
                guard,
                partial(_search_one, slug, query, ctx, limit, use_browser=True),
            )
            rendered[slug] = result

            # One missing browser means every remaining store would say the same thing.
            if result.outcome is SearchOutcome.NEEDS_BROWSER:
                for remaining in slugs[slugs.index(slug) + 1 :]:
                    rendered[remaining] = result_for(remaining, result)
                break
    return rendered


def result_for(slug: str, template: SearchResult) -> SearchResult:
    """Repeat one store's verdict for another, keeping the message."""
    return SearchResult.failure(slug, template.outcome, template.message or "")


def _host_for(slug: str) -> str:
    """The host a store's search will hit, for throttling purposes."""
    config = _render_mode(slug)
    if config is None:
        return slug
    return host_of(config.url_template.format(query="x")) or slug
