"""Product Entry endpoints.

Thin, like every router here: they translate HTTP to service calls and back, and let
``api/errors.py`` map domain exceptions to status codes. Nothing below decides anything.

One shape recurs and is deliberate: everything is reported *per retailer*. Amazon failing
tells you nothing about Flipkart, so a merged status would hide the half that worked, and a
merged price series would splice two shops' observations into a history that never
happened.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from ...db.models import CheckExecution, ProductEntry, RetailerListing
from ...domain.enums import ProductEntryStatus
from ...services.check_runner import run_check
from ...services.history_service import HistoryService
from ...services.product_entry_service import ListingInput, ProductEntryService
from ...stores.catalogue import STORES_BY_SLUG
from ...stores.registry import default_registry
from ..deps import Config, CurrentReader, CurrentUser, DbSession, PageParams, RequireWrite
from ..schemas.common import ErrorResponse, Page
from ..schemas.product_entries import (
    AvailabilityPoint,
    EntryCheckResponse,
    EntryHistoryResponse,
    EntryStatsResponse,
    ListingCheckResult,
    ListingHistory,
    ListingResponse,
    ListingStats,
    ListingUpdate,
    PricePoint,
    ProductEntryCreate,
    ProductEntryResponse,
    ProductEntryUpdate,
)

router = APIRouter(prefix="/product-entries", tags=["product-entries"])

_NOT_FOUND = {"model": ErrorResponse, "description": "No such entry, for this account."}
_CONFLICT = {
    "model": ErrorResponse,
    "description": "This URL is already live in one of your entries.",
}
_BAD_URL = {
    "model": ErrorResponse,
    "description": "URL rejected: wrong retailer, malformed, or blocked by the SSRF guard.",
}


def _service(
    session: DbSession, settings: Config, user_id: int
) -> ProductEntryService:
    return ProductEntryService(session, default_registry(), settings, user_id)


def _store_name(slug: str) -> str:
    info = STORES_BY_SLUG.get(slug)
    return info.display_name if info else slug


def _last_executions(
    session: DbSession, product_ids: Sequence[int]
) -> dict[int, CheckExecution]:
    """The most recent check for each of these products, in one query.

    This was one query *per listing*, which is invisible until there is a page of them:
    a 40-entry list cost 85 queries and the response looked perfectly correct either way.
    ``DISTINCT ON`` is PostgreSQL's idiom for "the top row of each group" and it walks
    ``ix_check_executions_product_id_started_at`` rather than sorting the table.
    """
    if not product_ids:
        return {}
    stmt = (
        select(CheckExecution)
        .where(CheckExecution.product_id.in_(set(product_ids)))
        .order_by(CheckExecution.product_id, CheckExecution.started_at.desc())
        .distinct(CheckExecution.product_id)
    )
    return {row.product_id: row for row in session.execute(stmt).scalars()}


def _listing_response(
    listing: RetailerListing, executions: Mapping[int, CheckExecution]
) -> ListingResponse:
    product = listing.product
    execution = executions.get(listing.product_id)
    return ListingResponse(
        id=listing.id,
        store=listing.store_slug,
        store_name=_store_name(listing.store_slug),
        product_name=listing.product_name,
        url=product.url,
        product_id=product.id,
        price=product.current_price,
        currency=product.currency,
        availability=product.availability,
        tracking_status=product.tracking_status,
        last_checked_at=product.last_checked_at,
        last_check_status=execution.status if execution else None,
        last_check_error=execution.error_type if execution else None,
        is_active=listing.is_active,
        deactivated_at=listing.deactivated_at,
    )


def _entry_response(
    session: DbSession,
    entry: ProductEntry,
    executions: Mapping[int, CheckExecution] | None = None,
) -> ProductEntryResponse:
    """One entry as the API returns it.

    ``executions`` lets a caller rendering many entries look the checks up once for the
    whole page; omitted, it is looked up for this entry alone -- still one query, not one
    per listing.
    """
    listings = list(entry.listings)
    if executions is None:
        executions = _last_executions(session, [item.product_id for item in listings])
    return ProductEntryResponse(
        id=entry.id,
        product_name=entry.canonical_name,
        status=entry.status,
        listings=[_listing_response(item, executions) for item in listings],
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        deleted_at=entry.deleted_at,
    )


def _entry_page(session: DbSession, entries: Sequence[ProductEntry]) -> list[ProductEntryResponse]:
    """A page of entries, with every listing's last check fetched in a single query."""
    executions = _last_executions(
        session, [item.product_id for entry in entries for item in entry.listings]
    )
    return [_entry_response(session, entry, executions) for entry in entries]


@router.post(
    "",
    response_model=ProductEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Product Entry with its Amazon and Flipkart listings",
    dependencies=[RequireWrite],
    responses={409: _CONFLICT, 422: _BAD_URL},
)
def create_entry(
    payload: ProductEntryCreate,
    session: DbSession,
    settings: Config,
    user: CurrentUser,
) -> ProductEntryResponse:
    """Create one entry and both of its listings, in one transaction.

    Returns before either retailer has been read, so the listings come back with a null
    price. Making the caller wait on two shops would turn a form submission into a
    thirty-second stare at a spinner and tie this response's latency to whichever retailer
    is slowest today.

    The first prices arrive on their own: the worker reconciles every
    ``RECONCILE_INTERVAL_SECONDS`` (60 by default), picks up any product whose tracking
    status is active, and schedules it with a small per-product offset. Nothing here has to
    ask it to. A caller who wants a price *now* can POST to ``/check``, which is
    synchronous and says so.
    """
    service = _service(session, settings, user.id)
    entry = service.create(
        payload.product_name,
        amazon=ListingInput(payload.amazon.product_name, payload.amazon.url),
        flipkart=ListingInput(payload.flipkart.product_name, payload.flipkart.url),
    )
    session.flush()
    return _entry_response(session, entry)


@router.get(
    "",
    response_model=Page[ProductEntryResponse],
    summary="List your Product Entries",
)
def list_entries(
    session: DbSession,
    settings: Config,
    user: CurrentReader,
    page: PageParams,
    entry_status: Annotated[
        ProductEntryStatus | None,
        Query(alias="status", description="Filter by active or archived."),
    ] = None,
) -> Page[ProductEntryResponse]:
    result = _service(session, settings, user.id).list(
        limit=page.limit, offset=page.offset, status=entry_status
    )
    return Page[ProductEntryResponse](
        items=_entry_page(session, result.items),
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/{entry_id}",
    response_model=ProductEntryResponse,
    summary="One Product Entry, with each retailer's current state",
    responses={404: _NOT_FOUND},
)
def get_entry(
    entry_id: int, session: DbSession, settings: Config, user: CurrentReader
) -> ProductEntryResponse:
    entry = _service(session, settings, user.id).get(entry_id)
    return _entry_response(session, entry)


@router.patch(
    "/{entry_id}",
    response_model=ProductEntryResponse,
    summary="Rename a Product Entry",
    dependencies=[RequireWrite],
    responses={404: _NOT_FOUND},
)
def update_entry(
    entry_id: int,
    payload: ProductEntryUpdate,
    session: DbSession,
    settings: Config,
    user: CurrentUser,
) -> ProductEntryResponse:
    """Change the canonical name. The entry keeps its id and all of its history."""
    entry = _service(session, settings, user.id).update(
        entry_id, canonical_name=payload.canonical_name
    )
    return _entry_response(session, entry)


@router.delete(
    "/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a Product Entry",
    dependencies=[RequireWrite],
    responses={404: _NOT_FOUND},
)
def archive_entry(
    entry_id: int, session: DbSession, settings: Config, user: CurrentUser
) -> None:
    """Archive rather than delete. Scheduled checks stop; every observation stays
    readable, because "I stopped following this" is not "this never happened"."""
    _service(session, settings, user.id).archive(entry_id)


@router.post(
    "/{entry_id}/check",
    response_model=EntryCheckResponse,
    summary="Check every retailer of an entry now",
    dependencies=[RequireWrite],
    responses={404: _NOT_FOUND},
)
def check_entry(
    entry_id: int, session: DbSession, settings: Config, user: CurrentUser
) -> EntryCheckResponse:
    """Check each live listing and report each one separately.

    200 even when every retailer failed: a recorded failure is a successfully observed
    fact, and a 5xx here would claim this API broke when it did not.
    """
    return _run_entry_check(entry_id, session, settings, user.id, listing_id=None)


@router.post(
    "/{entry_id}/listings/{listing_id}/check",
    response_model=EntryCheckResponse,
    summary="Check one retailer of an entry now",
    dependencies=[RequireWrite],
    responses={404: _NOT_FOUND},
)
def check_listing(
    entry_id: int,
    listing_id: int,
    session: DbSession,
    settings: Config,
    user: CurrentUser,
) -> EntryCheckResponse:
    return _run_entry_check(entry_id, session, settings, user.id, listing_id=listing_id)


def _run_entry_check(
    entry_id: int,
    session: DbSession,
    settings: Config,
    user_id: int,
    *,
    listing_id: int | None,
) -> EntryCheckResponse:
    service = _service(session, settings, user_id)
    listings = {
        item.product_id: item
        for item in service.active_listings(entry_id)
        if listing_id is None or item.id == listing_id
    }
    if listing_id is not None and not listings:
        # Either it is not this entry's listing, or it has been removed. `product_ids`
        # raises the right NotFoundError for the first case.
        service.product_ids(entry_id, listing_id=listing_id)

    results: list[ListingCheckResult] = []
    for product_id, listing in listings.items():
        # Runs in its own transactions, so a slow provider never holds this request's
        # session open. Each retailer is checked independently: one failing does not stop
        # the next.
        outcome = run_check(product_id, settings=settings, registry=default_registry())
        execution = session.get(CheckExecution, outcome.execution_id)
        results.append(
            ListingCheckResult(
                listing_id=listing.id,
                store=listing.store_slug,
                status=outcome.status,
                price=execution.extracted_price if execution else None,
                currency=execution.extracted_currency if execution else None,
                availability=outcome.availability,
                error_type=execution.error_type if execution else None,
                error_detail=execution.error_detail if execution else None,
            )
        )
    return EntryCheckResponse(product_entry_id=entry_id, results=results)


@router.get(
    "/{entry_id}/history",
    response_model=EntryHistoryResponse,
    summary="Price and availability history, one section per retailer",
    responses={404: _NOT_FOUND},
)
def entry_history(
    entry_id: int,
    session: DbSession,
    settings: Config,
    user: CurrentReader,
    page: PageParams,
) -> EntryHistoryResponse:
    """Newest first, and never merged across retailers.

    Two shops' observations are two series. Interleaving them would produce a price history
    that no single listing ever had.
    """
    service = _service(session, settings, user.id)
    entry = service.get(entry_id)
    history = HistoryService(session)

    sections: list[ListingHistory] = []
    for listing in entry.listings:
        prices = history.price_history(
            listing.product_id, limit=page.limit, offset=page.offset
        )
        availability = history.availability_history(
            listing.product_id, limit=page.limit, offset=page.offset
        )
        sections.append(
            ListingHistory(
                listing_id=listing.id,
                store=listing.store_slug,
                store_name=_store_name(listing.store_slug),
                prices=[
                    PricePoint(
                        price=row.price, currency=row.currency, observed_at=row.observed_at
                    )
                    for row in prices.items
                ],
                availability=[
                    AvailabilityPoint(
                        availability=row.availability, observed_at=row.observed_at
                    )
                    for row in availability.items
                ],
            )
        )
    return EntryHistoryResponse(product_entry_id=entry_id, listings=sections)


@router.get(
    "/{entry_id}/stats",
    response_model=EntryStatsResponse,
    summary="Per-retailer price statistics",
    responses={404: _NOT_FOUND},
)
def entry_stats(
    entry_id: int, session: DbSession, settings: Config, user: CurrentReader
) -> EntryStatsResponse:
    """Statistics per retailer. Cross-retailer comparison is a presentation concern --
    a merged historical series across two shops is not a thing that existed."""
    service = _service(session, settings, user.id)
    entry = service.get(entry_id)
    history = HistoryService(session)

    rows: list[ListingStats] = []
    for listing in entry.listings:
        stats = history.stats(listing.product_id)
        rows.append(
            ListingStats(
                listing_id=listing.id,
                store=listing.store_slug,
                store_name=_store_name(listing.store_slug),
                currency=stats.currency if stats else None,
                observations=stats.observations if stats else 0,
                current=stats.current if stats else None,
                lowest=stats.lowest if stats else None,
                highest=stats.highest if stats else None,
                average=stats.average if stats else None,
                lowest_at=stats.lowest_at if stats else None,
                first_observed_at=stats.first_observed_at if stats else None,
                changed_by=stats.changed_by if stats else None,
                mixed_currency=stats.mixed_currency if stats else False,
            )
        )
    return EntryStatsResponse(product_entry_id=entry_id, listings=rows)


@router.post(
    "/{entry_id}/pause",
    response_model=ProductEntryResponse,
    summary="Stop scheduled checks for every retailer of an entry",
    dependencies=[RequireWrite],
    responses={404: _NOT_FOUND},
)
def pause_entry(
    entry_id: int, session: DbSession, settings: Config, user: CurrentUser
) -> ProductEntryResponse:
    service = _service(session, settings, user.id)
    service.set_tracking(entry_id, active=False)
    return _entry_response(session, service.get(entry_id))


@router.post(
    "/{entry_id}/resume",
    response_model=ProductEntryResponse,
    summary="Resume scheduled checks for an entry",
    dependencies=[RequireWrite],
    responses={404: _NOT_FOUND},
)
def resume_entry(
    entry_id: int, session: DbSession, settings: Config, user: CurrentUser
) -> ProductEntryResponse:
    service = _service(session, settings, user.id)
    service.set_tracking(entry_id, active=True)
    return _entry_response(session, service.get(entry_id))


@router.patch(
    "/{entry_id}/listings/{listing_id}",
    response_model=ListingResponse,
    summary="Change a retailer listing's name or URL",
    dependencies=[RequireWrite],
    responses={404: _NOT_FOUND, 409: _CONFLICT, 422: _BAD_URL},
)
def update_listing(
    entry_id: int,
    listing_id: int,
    payload: ListingUpdate,
    session: DbSession,
    settings: Config,
    user: CurrentUser,
) -> ListingResponse:
    """A name change is metadata. A URL change re-points the listing at a new tracking
    target while keeping the listing's id -- observations already recorded stay attached
    to the URL that produced them, because that is where they were seen."""
    listing = _service(session, settings, user.id).update_listing(
        entry_id, listing_id, product_name=payload.product_name, url=payload.url
    )
    session.flush()
    session.refresh(listing)
    return _listing_response(listing, _last_executions(session, [listing.product_id]))


@router.delete(
    "/{entry_id}/listings/{listing_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove one retailer from an entry",
    dependencies=[RequireWrite],
    responses={404: _NOT_FOUND},
)
def deactivate_listing(
    entry_id: int,
    listing_id: int,
    session: DbSession,
    settings: Config,
    user: CurrentUser,
) -> None:
    """The entry and the other retailer are untouched, and this retailer's observations
    stay readable. Deactivated rather than deleted."""
    _service(session, settings, user.id).deactivate_listing(entry_id, listing_id)

