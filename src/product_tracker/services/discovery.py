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

from dataclasses import dataclass, field

from ..core.config import Settings
from ..core.logging import get_logger
from ..domain.enums import SearchOutcome
from ..domain.models import FetchContext, SearchHit, SearchResult
from ..stores.catalogue import KNOWN_STORES
from ..stores.search import available_searches, search_for

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
) -> Discovery:
    """Search every configured store for ``query``.

    One request per store, sequentially. A store that fails contributes a failed result
    rather than an exception, so one blocked retailer never costs the others their answers
    -- the same contract product checks hold themselves to.
    """
    targets = store_slugs if store_slugs is not None else searchable_stores()
    ctx = FetchContext(
        timeout_seconds=settings.http_timeout_seconds,
        # Search is a fan-out: one query is already N requests. Rendering each of them in
        # a browser turns a two-second answer into a minute of someone else's CPU.
        allow_browser=False,
        user_agent=settings.http_user_agent,
    )

    results: list[SearchResult] = []
    for slug in targets:
        search = search_for(slug)
        if search is None:
            results.append(
                SearchResult.failure(
                    slug, SearchOutcome.UNSUPPORTED, "no search is configured for this store"
                )
            )
            continue
        results.append(search.search(query, ctx, limit=limit_per_store))

    discovery = Discovery(query=query, results=tuple(results))
    log.info(
        "discovery.completed",
        query_length=len(query),
        stores=len(results),
        hits=len(discovery.hits),
        exact=len(discovery.exact),
    )
    return discovery
