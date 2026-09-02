"""The Product Entry pages.

Server-rendered HTML over the same :class:`ProductEntryService` the API and CLI use. There
is no business logic here on purpose -- if this module started deciding things, the page and
the API would eventually disagree about what a Product Entry is, and the page would win
because that is what people look at.

Two behaviours are worth stating because they are easy to get wrong:

* **A rejected form comes back with every field still filled in.** Making someone retype
  five fields because one was wrong is a small cruelty that stops people using a thing.
* **Every non-success state is rendered as itself.** A shop that refused us, a page we
  could not parse, and a product that is genuinely sold out are three different facts, and
  ``presenters`` keeps them apart all the way to the markup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..api.deps import API_KEY_HEADER, Config, DbSession
from ..db.models import ProductEntry, RetailerListing, User
from ..domain.enums import CheckStatus, ProductEntryStatus, TrackingStatus
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..services.check_runner import run_check
from ..services.history_service import HistoryService
from ..services.product_entry_service import ListingInput, ProductEntryService
from ..services.user_service import default_user, resolve_user
from ..stores.catalogue import STORES_BY_SLUG
from ..stores.registry import default_registry
from . import presenters

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)

#: How many price rows the detail page shows per shop. Enough to see a trend, few enough
#: that the page stays a page.
HISTORY_ROWS = 10


@dataclass(frozen=True, slots=True)
class ListingView:
    """One listing, flattened for a template. Templates should not walk the ORM."""

    id: int
    store_slug: str
    store_name: str
    product_name: str
    url: str
    price_text: str
    view: presenters.StateView
    last_checked: str | None
    last_status: str | None
    is_active: bool


def web_user(request: Request, session: DbSession, settings: Config) -> User:
    """The account a page acts as.

    Takes the key from the ``X-API-Key`` header *or* a ``pt_key`` cookie, because a browser
    form cannot set a header. With authentication off -- the localhost default -- this is
    the default account and the pages simply work.
    """
    api_key = request.headers.get(API_KEY_HEADER) or request.cookies.get("pt_key")
    return resolve_user(session, settings, api_key) or default_user(session)


#: Declared as a dependency rather than fetched by hand. An earlier version called
#: ``next(get_db())``, which starts the generator and never closes it: every page leaked a
#: connection until the pool was exhausted and every later request blocked forever.
WebUser = Annotated[User, Depends(web_user)]


def _service(session: Any, settings: Any, user_id: int) -> ProductEntryService:
    return ProductEntryService(session, default_registry(), settings, user_id)


def _store_name(slug: str) -> str:
    info = STORES_BY_SLUG.get(slug)
    return info.display_name if info else slug


def _last_check(session: Any, product_id: int) -> tuple[CheckStatus | None, str | None]:
    from sqlalchemy import select

    from ..db.models import CheckExecution

    stmt = (
        select(CheckExecution)
        .where(CheckExecution.product_id == product_id)
        .order_by(CheckExecution.started_at.desc())
        .limit(1)
    )
    row = session.execute(stmt).scalars().first()
    return (row.status, row.error_type) if row else (None, None)


def _view(session: Any, listing: RetailerListing) -> ListingView:
    status, error = _last_check(session, listing.product_id)
    product = listing.product
    return ListingView(
        id=listing.id,
        store_slug=listing.store_slug,
        store_name=_store_name(listing.store_slug),
        product_name=listing.product_name,
        url=product.url,
        price_text=presenters.price_text(listing),
        view=presenters.describe(listing, last_status=status, last_error=error),
        last_checked=(
            product.last_checked_at.strftime("%Y-%m-%d %H:%M")
            if product.last_checked_at
            else None
        ),
        last_status=status.value if status else None,
        is_active=listing.is_active,
    )


# --- Listing -----------------------------------------------------------------------


@router.get("/products", response_class=HTMLResponse)
def list_page(
    request: Request,
    session: DbSession,
    settings: Config,
    user: WebUser,
    status: str | None = None,
) -> HTMLResponse:
    user_id = int(user.id)
    wanted = (
        ProductEntryStatus.ARCHIVED
        if status == "archived"
        else ProductEntryStatus.ACTIVE
    )
    page = _service(session, settings, user_id).list(limit=100, status=wanted)

    # A stable column per shop, so a product missing from one shop leaves a gap rather
    # than shifting every other row's columns along by one.
    columns = ["amazon-in", "flipkart"]
    rows = []
    for entry in page.items:
        by_store = {item.store_slug: item for item in entry.listings if item.is_active}
        rows.append(
            {
                "id": entry.id,
                "name": entry.canonical_name,
                "cells": [
                    _view(session, by_store[slug]) if slug in by_store else None
                    for slug in columns
                ],
            }
        )

    return TEMPLATES.TemplateResponse(
        request,
        "entry_list.html",
        {
            "entries": rows,
            "total": page.total,
            "store_columns": [_store_name(slug) for slug in columns],
            "showing_archived": wanted is ProductEntryStatus.ARCHIVED,
        },
    )


# --- Creating ----------------------------------------------------------------------


@router.get("/products/new", response_class=HTMLResponse)
def new_form(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "add_product.html", {"values": {}, "errors": []}
    )


@router.post("/products")
def submit_form(
    request: Request,
    session: DbSession,
    settings: Config,
    user: WebUser,
    product_name: str = Form(default=""),
    amazon_name: str = Form(default=""),
    amazon_url: str = Form(default=""),
    flipkart_name: str = Form(default=""),
    flipkart_url: str = Form(default=""),
) -> Any:
    user_id = int(user.id)
    values = {
        "product_name": product_name,
        "amazon_name": amazon_name,
        "amazon_url": amazon_url,
        "flipkart_name": flipkart_name,
        "flipkart_url": flipkart_url,
    }

    missing = [label for label, value in values.items() if not value.strip()]
    if missing:
        return _reject(request, values, ["Every field is required."])

    try:
        entry = _service(session, settings, user_id).create(
            product_name,
            amazon=ListingInput(amazon_name, amazon_url),
            flipkart=ListingInput(flipkart_name, flipkart_url),
        )
    except (ValidationError, DuplicateError) as exc:
        session.rollback()
        # The server's reason, verbatim. "Invalid input" would tell the user nothing about
        # which of the five fields to fix.
        return _reject(request, values, [str(exc)])

    # 303, so a refresh of the destination does not repost the form.
    return RedirectResponse(f"/ui/products/{entry.id}", status_code=303)


def _reject(request: Request, values: dict[str, str], errors: list[str]) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "add_product.html",
        {"values": values, "errors": errors},
        status_code=422,
    )


# --- Detail ------------------------------------------------------------------------


@router.get("/products/{entry_id}", response_class=HTMLResponse)
def detail_page(
    request: Request,
    entry_id: int,
    session: DbSession,
    settings: Config,
    user: WebUser,
    notice: str | None = None,
) -> HTMLResponse:
    user_id = int(user.id)
    service = _service(session, settings, user_id)
    try:
        entry = service.get(entry_id)
    except NotFoundError:
        return _not_found(request)

    views = [_view(session, item) for item in entry.listings]
    active = [item for item in views if item.is_active]
    live = [item for item in entry.listings if item.is_active]
    cheapest_id = presenters.cheapest(entry.listings)

    priced = [item for item in live if item.product.current_price is not None]
    currencies = {item.product.currency for item in priced}

    return TEMPLATES.TemplateResponse(
        request,
        "entry_detail.html",
        {
            "entry": entry,
            "entry_id": entry.id,
            "listings": views,
            "active_listings": active,
            "cheapest_id": cheapest_id,
            "comparable": len(active) > 1,
            "mixed_currency": len(currencies) > 1,
            "paused": _is_paused(entry),
            "history": _history(session, entry),
            "notice": notice,
        },
    )


def _is_paused(entry: ProductEntry) -> bool:
    live = [item for item in entry.listings if item.is_active]
    return bool(live) and all(
        item.product.tracking_status is TrackingStatus.PAUSED for item in live
    )


def _history(session: Any, entry: ProductEntry) -> list[dict[str, Any]]:
    """Recent prices, one section per shop. Never interleaved."""
    service = HistoryService(session)
    sections = []
    for listing in entry.listings:
        page = service.price_history(listing.product_id, limit=HISTORY_ROWS, offset=0)
        sections.append(
            {
                "store_name": _store_name(listing.store_slug),
                "rows": [
                    {
                        "when": row.observed_at.strftime("%Y-%m-%d %H:%M"),
                        "price": f"{row.currency} {row.price:,.2f}",
                    }
                    for row in page.items
                ],
            }
        )
    return sections


def _not_found(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "entry_list.html",
        {
            "entries": [],
            "total": 0,
            "store_columns": [],
            "showing_archived": False,
        },
        status_code=404,
    )


# --- Editing -----------------------------------------------------------------------


@router.get("/products/{entry_id}/edit", response_class=HTMLResponse)
def edit_form(
    request: Request, entry_id: int, session: DbSession, settings: Config, user: WebUser
) -> HTMLResponse:
    user_id = int(user.id)
    try:
        entry = _service(session, settings, user_id).get(entry_id)
    except NotFoundError:
        return _not_found(request)
    return TEMPLATES.TemplateResponse(
        request,
        "entry_edit.html",
        {
            "entry": entry,
            "values": {"product_name": entry.canonical_name},
            "listings": [_view(session, item) for item in entry.listings if item.is_active],
            "errors": [],
        },
    )


@router.post("/products/{entry_id}")
async def submit_edit(
    request: Request, entry_id: int, session: DbSession, settings: Config, user: WebUser
) -> Any:
    user_id = int(user.id)
    service = _service(session, settings, user_id)
    try:
        entry = service.get(entry_id)
    except NotFoundError:
        return _not_found(request)

    form = await request.form()
    name = str(form.get("product_name", "")).strip()
    live = [item for item in entry.listings if item.is_active]

    try:
        if name:
            service.update(entry_id, canonical_name=name)
        for listing in live:
            new_name = str(form.get(f"name_{listing.id}", "")).strip()
            new_url = str(form.get(f"url_{listing.id}", "")).strip()
            service.update_listing(
                entry_id,
                listing.id,
                product_name=new_name or None,
                url=new_url or None,
            )
    except (ValidationError, DuplicateError, NotFoundError) as exc:
        session.rollback()
        entry = service.get(entry_id)
        return TEMPLATES.TemplateResponse(
            request,
            "entry_edit.html",
            {
                "entry": entry,
                "values": {"product_name": name or entry.canonical_name},
                "listings": [
                    _view(session, item) for item in entry.listings if item.is_active
                ],
                "errors": [str(exc)],
            },
            status_code=422,
        )

    return RedirectResponse(f"/ui/products/{entry_id}", status_code=303)


# --- Actions -----------------------------------------------------------------------


@router.post("/products/{entry_id}/listings/{listing_id}/check", response_class=HTMLResponse)
def check_one(
    request: Request,
    entry_id: int,
    listing_id: int,
    session: DbSession,
    settings: Config,
    user: WebUser,
) -> HTMLResponse:
    """Check one shop and swap that panel back in.

    Only that panel: the other retailer's state is not re-read, so a check here can never
    make the neighbouring column appear to change on its own.
    """
    user_id = int(user.id)
    service = _service(session, settings, user_id)
    try:
        product_ids = service.product_ids(entry_id, listing_id=listing_id)
    except NotFoundError:
        return HTMLResponse("<article class='panel'>No such listing.</article>", 404)

    for product_id in product_ids:
        run_check(product_id, settings=settings, registry=default_registry())

    session.expire_all()
    entry = service.get(entry_id)
    listing = next(item for item in entry.listings if item.id == listing_id)
    return TEMPLATES.TemplateResponse(
        request,
        "_retailer_panel.html",
        {
            "listing": _view(session, listing),
            "entry_id": entry_id,
            "is_cheapest": presenters.cheapest(entry.listings) == listing_id,
        },
    )


@router.post("/products/{entry_id}/check")
def check_all(
    request: Request, entry_id: int, session: DbSession, settings: Config, user: WebUser
) -> Any:
    user_id = int(user.id)
    service = _service(session, settings, user_id)
    try:
        product_ids = service.product_ids(entry_id)
    except NotFoundError:
        return _not_found(request)
    for product_id in product_ids:
        run_check(product_id, settings=settings, registry=default_registry())
    return RedirectResponse(f"/ui/products/{entry_id}", status_code=303)


@router.post("/products/{entry_id}/pause")
def pause(
    request: Request, entry_id: int, session: DbSession, settings: Config, user: WebUser
) -> Any:
    return _set_tracking(request, entry_id, session, settings, user, active=False)


@router.post("/products/{entry_id}/resume")
def resume(
    request: Request, entry_id: int, session: DbSession, settings: Config, user: WebUser
) -> Any:
    return _set_tracking(request, entry_id, session, settings, user, active=True)


def _set_tracking(
    request: Request,
    entry_id: int,
    session: DbSession,
    settings: Config,
    user: WebUser,
    *,
    active: bool,
) -> Any:
    user_id = int(user.id)
    try:
        _service(session, settings, user_id).set_tracking(entry_id, active=active)
    except NotFoundError:
        return _not_found(request)
    return RedirectResponse(f"/ui/products/{entry_id}", status_code=303)


@router.post("/products/{entry_id}/archive")
def archive(
    request: Request, entry_id: int, session: DbSession, settings: Config, user: WebUser
) -> Any:
    user_id = int(user.id)
    try:
        _service(session, settings, user_id).archive(entry_id)
    except NotFoundError:
        return _not_found(request)
    return RedirectResponse("/ui/products", status_code=303)
