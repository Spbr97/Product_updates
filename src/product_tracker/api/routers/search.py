"""Search endpoints: finding a product by name, so nobody has to paste URLs.

**Why this is a POST.** Nothing here changes our own state, so REST says GET. But a search
fans out to every configured retailer, and the rate limiter in ``api/ratelimit.py``
deliberately exempts GET -- "reads are cheap and local". A search is neither. Exposed as a
GET it would be the one unmetered route in the API that causes outbound traffic to real
shops, which is precisely the failure mode that limiter exists to prevent. POST puts it
back under the limit.

**Why it needs a write key.** For the same reason: an anonymous caller should not be able
to spend this deployment's outbound budget, or its standing with a retailer.
"""

from __future__ import annotations

from fastapi import APIRouter

from ...core.logging import get_logger
from ...domain.errors import ValidationError
from ...domain.models import SearchHit, SearchResult
from ...services import discovery
from ...stores.catalogue import STORES_BY_SLUG
from ..deps import Config, CurrentUser, RequireWrite
from ..schemas.common import ErrorResponse
from ..schemas.search import (
    SearchHitResponse,
    SearchRequest,
    SearchResponse,
    StoreGapResponse,
)

log = get_logger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


def _store_name(slug: str) -> str:
    info = STORES_BY_SLUG.get(slug)
    return info.display_name if info else slug


def _hit(hit: SearchHit) -> SearchHitResponse:
    return SearchHitResponse(
        url=hit.url,
        title=hit.title,
        store=_store_name(hit.store_slug),
        store_slug=hit.store_slug,
        price=hit.price,
        currency=hit.currency,
        image_url=hit.image_url,
        score=hit.score,
        qualifiers=list(hit.qualifiers),
        is_exact=hit.is_exact,
        from_sitemap=hit.from_sitemap,
    )


def _gap(result: SearchResult) -> StoreGapResponse:
    return StoreGapResponse(
        store=_store_name(result.store_slug),
        store_slug=result.store_slug,
        outcome=result.outcome,
        message=result.message,
        http_status=result.http_status,
    )


def _targets(requested: list[str] | None) -> tuple[str, ...]:
    """Which stores to search, refusing names we cannot honour.

    An unknown slug is rejected rather than ignored. Dropping it silently would answer a
    request for one shop with results from none and a 200, which reads as "that shop has
    nothing" -- the exact confusion ``SearchOutcome`` exists to prevent.
    """
    searchable = discovery.searchable_stores()
    if requested is None:
        return searchable

    known = set(searchable)
    unknown = [slug for slug in requested if slug not in known]
    if unknown:
        raise ValidationError(
            f"no search is configured for: {', '.join(sorted(unknown))}. "
            f"Searchable stores: {', '.join(searchable)}"
        )
    if not requested:
        raise ValidationError("'stores' was empty; omit it to search every store")
    # De-duplicated, in catalogue order, so ten copies of one slug is not ten searches.
    chosen = set(requested)
    return tuple(slug for slug in searchable if slug in chosen)


@router.post(
    "",
    response_model=SearchResponse,
    summary="Search the shops for a product",
    dependencies=[RequireWrite],
    responses={
        422: {"model": ErrorResponse, "description": "Unknown store slug, or bad query."},
        429: {"model": ErrorResponse, "description": "Rate limited."},
    },
)
def search_products(
    payload: SearchRequest, settings: Config, user: CurrentUser
) -> SearchResponse:
    """Search every configured shop and return ranked candidates.

    Nothing is tracked. The caller picks a hit and posts its URL to ``/products``, which is
    deliberate: a Galaxy S25 and a Galaxy S25 FE both match a search for "Galaxy S25", and
    no ranking function can know which was meant. ``is_exact`` and ``qualifiers`` are what
    a client shows so a person can tell them apart.

    Slow by nature -- several retailers, paced so as not to hammer any of them. Expect a
    few seconds, and considerably longer with ``allow_browser``.
    """
    targets = _targets(payload.stores)

    found = discovery.discover(
        payload.query,
        settings,
        store_slugs=targets,
        limit_per_store=payload.limit_per_store,
        allow_browser=payload.allow_browser,
    )

    # The query itself is not logged: it is a person's shopping intent, and this project
    # keeps that out of the logs the same way it keeps URLs' query strings out.
    log.info(
        "api.search",
        user_id=user.id,
        query_length=len(payload.query),
        stores=len(targets),
        hits=len(found.hits),
        exact=len(found.exact),
    )

    return SearchResponse(
        query=payload.query,
        hits=[_hit(hit) for hit in found.hits],
        exact_count=len(found.exact),
        searched=list(targets),
        gaps=[_gap(result) for result in found.unsearchable],
        skipped=list(discovery.unsearchable_stores()),
    )
