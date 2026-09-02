"""Product Entry commands: one logical product, one listing per retailer.

Every command here calls :class:`ProductEntryService`. Nothing decides anything locally,
so the CLI and the API cannot drift into disagreeing about what a Product Entry is.

The display rule worth knowing: a retailer that could not be read is never shown as sold
out. "blocked", "no price" and "sold out" are three different facts, and collapsing them
would make the one number a person acts on untrustworthy.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import typer

from ..db.session import session_scope
from ..domain.enums import Availability, CheckStatus, ProductEntryStatus, TrackingStatus
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..services.check_runner import run_check
from ..services.product_entry_service import ListingInput, ProductEntryService
from ..stores.catalogue import STORES_BY_SLUG
from ..stores.registry import default_registry
from ..utils.money import format_money_short
from .formatting import ExitCode, error, info, stdout, success, table
from .users import UserOption, acting_user

entries_app = typer.Typer(
    help="Track one product across Amazon and Flipkart together.", no_args_is_help=True
)

def _service(session: object, user_id: int) -> ProductEntryService:
    from ..core.config import get_settings

    return ProductEntryService(session, default_registry(), get_settings(), user_id)  # type: ignore[arg-type]


def _store_name(slug: str) -> str:
    info_ = STORES_BY_SLUG.get(slug)
    return info_.display_name if info_ else slug


def _state_text(listing: object) -> tuple[str, str]:
    """What one retailer currently says, and how confident we are that it said it."""
    product = listing.product  # type: ignore[attr-defined]
    if not listing.is_active:  # type: ignore[attr-defined]
        return "removed", "dim"
    if product.tracking_status is TrackingStatus.PAUSED:
        return "paused", "dim"
    if product.current_price is not None:
        return format_money_short(product.current_price, product.currency), "green"
    if product.last_checked_at is None:
        return "pending", "dim"
    if product.availability is Availability.OUT_OF_STOCK:
        return "sold out", "red"
    return "no price", "yellow"


def _availability_text(listing: object) -> str:
    product = listing.product  # type: ignore[attr-defined]
    if product.availability is Availability.IN_STOCK:
        return "in stock"
    if product.availability is Availability.OUT_OF_STOCK:
        return "sold out"
    # Unknown is a real answer, and the honest one when nothing said otherwise.
    return "unknown"


@entries_app.command("add")
def add_entry(
    name: Annotated[str, typer.Option("--name", help="What you call this product.")],
    amazon_name: Annotated[str, typer.Option("--amazon-name")],
    amazon_url: Annotated[str, typer.Option("--amazon-url")],
    flipkart_name: Annotated[str, typer.Option("--flipkart-name")],
    flipkart_url: Annotated[str, typer.Option("--flipkart-url")],
    user: UserOption = None,
) -> None:
    """Create one entry with its Amazon and Flipkart listings.

    Both retailers are required: an entry with one shop cannot compare anything, which is
    the point of the thing. Prices arrive on the worker's next pass, not now.
    """
    with session_scope() as session:
        owner = acting_user(session, user)
        try:
            entry = _service(session, owner.id).create(
                name,
                amazon=ListingInput(amazon_name, amazon_url),
                flipkart=ListingInput(flipkart_name, flipkart_url),
            )
        except DuplicateError as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.ERROR) from exc
        except ValidationError as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.ERROR) from exc

        entry_id = entry.id
        shops = ", ".join(_store_name(x.store_slug) for x in entry.listings)

    success(f"Product Entry {entry_id} created across {shops}.")
    info("Prices arrive with the next scheduled check. 'entries check' does it now.")


@entries_app.command("list")
def list_entries(
    archived: Annotated[
        bool, typer.Option("--archived", help="Show archived entries instead.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
    user: UserOption = None,
) -> None:
    """List your Product Entries and what each retailer currently says."""
    wanted = ProductEntryStatus.ARCHIVED if archived else ProductEntryStatus.ACTIVE
    with session_scope() as session:
        owner = acting_user(session, user)
        page = _service(session, owner.id).list(limit=limit, status=wanted)

        if not page.items:
            info("No product entries yet. Create one with 'entries add'.")
            return

        listing = table(
            f"Product entries ({page.total})", ["ID", "Product", "Shop", "Price", "Stock"]
        )
        for entry in page.items:
            first = True
            for item in entry.listings:
                text, colour = _state_text(item)
                listing.add_row(
                    str(entry.id) if first else "",
                    entry.canonical_name if first else "",
                    _store_name(item.store_slug),
                    f"[{colour}]{text}[/{colour}]",
                    _availability_text(item),
                )
                first = False
        stdout.print(listing)


@entries_app.command("show")
def show_entry(
    entry_id: Annotated[int, typer.Argument(help="Product Entry id.")],
    user: UserOption = None,
) -> None:
    """One entry, retailer by retailer, with the shops side by side."""
    with session_scope() as session:
        owner = acting_user(session, user)
        try:
            entry = _service(session, owner.id).get(entry_id)
        except NotFoundError as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.NOT_FOUND) from exc

        stdout.print(f"\n[bold]{entry.canonical_name}[/bold]  (entry {entry.id})")
        stdout.print(f"[dim]status: {entry.status.value}[/dim]\n")

        detail = table(
            "Retailers", ["Shop", "Your name", "Price", "Stock", "Last checked", "URL"]
        )
        for item in entry.listings:
            text, colour = _state_text(item)
            checked = (
                item.product.last_checked_at.strftime("%Y-%m-%d %H:%M")
                if item.product.last_checked_at
                else "never"
            )
            detail.add_row(
                _store_name(item.store_slug),
                item.product_name,
                f"[{colour}]{text}[/{colour}]",
                _availability_text(item),
                checked,
                item.product.url[:60],
            )
        stdout.print(detail)


@entries_app.command("edit")
def edit_entry(
    entry_id: Annotated[int, typer.Argument(help="Product Entry id.")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    amazon_name: Annotated[str | None, typer.Option("--amazon-name")] = None,
    amazon_url: Annotated[str | None, typer.Option("--amazon-url")] = None,
    flipkart_name: Annotated[str | None, typer.Option("--flipkart-name")] = None,
    flipkart_url: Annotated[str | None, typer.Option("--flipkart-url")] = None,
    user: UserOption = None,
) -> None:
    """Change the entry's name, or a retailer's name or URL.

    The entry keeps its id. Changing a URL re-points that listing at a new page; the
    observations already recorded stay attached to the URL that produced them, because
    that is where they were seen.
    """
    if not any([name, amazon_name, amazon_url, flipkart_name, flipkart_url]):
        error("nothing to change; pass at least one option")
        raise typer.Exit(ExitCode.ERROR)

    with session_scope() as session:
        owner = acting_user(session, user)
        service = _service(session, owner.id)
        try:
            if name:
                service.update(entry_id, canonical_name=name)
            entry = service.get(entry_id)
            by_store = {x.store_slug: x for x in entry.listings if x.is_active}
            for slug, new_name, new_url in (
                ("amazon-in", amazon_name, amazon_url),
                ("flipkart", flipkart_name, flipkart_url),
            ):
                if new_name is None and new_url is None:
                    continue
                target = by_store.get(slug)
                if target is None:
                    error(f"this entry has no active {_store_name(slug)} listing")
                    raise typer.Exit(ExitCode.NOT_FOUND)
                service.update_listing(
                    entry_id, target.id, product_name=new_name, url=new_url
                )
        except NotFoundError as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.NOT_FOUND) from exc
        except (DuplicateError, ValidationError) as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.ERROR) from exc

    success(f"Product Entry {entry_id} updated. Its id and history are unchanged.")


@entries_app.command("check")
def check_entry(
    entry_id: Annotated[int, typer.Argument(help="Product Entry id.")],
    retailer: Annotated[
        str | None,
        typer.Option("--retailer", help="Check one shop only, by slug."),
    ] = None,
    user: UserOption = None,
) -> None:
    """Check the entry's retailers now, and report each one separately.

    Amazon failing says nothing about Flipkart, so the two outcomes are printed apart. An
    unreadable shop is reported as unreadable, never as sold out.
    """
    from ..core.config import get_settings

    settings = get_settings()
    with session_scope() as session:
        owner = acting_user(session, user)
        service = _service(session, owner.id)
        try:
            listings = [
                item
                for item in service.active_listings(entry_id)
                if retailer is None or item.store_slug == retailer
            ]
        except NotFoundError as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.NOT_FOUND) from exc
        if not listings:
            error("no active listing matched")
            raise typer.Exit(ExitCode.NOT_FOUND)
        targets = [(item.product_id, item.store_slug) for item in listings]

    results = table("Check", ["Shop", "Result", "Price", "Stock"])
    any_failed = False
    for product_id, slug in targets:
        outcome = run_check(product_id, settings=settings, registry=default_registry())
        ok = outcome.status is CheckStatus.SUCCESS
        any_failed = any_failed or not ok
        results.add_row(
            _store_name(slug),
            f"[green]{outcome.status.value}[/green]"
            if ok
            else f"[yellow]{outcome.status.value}[/yellow]",
            # CheckOutcome carries the price as a string: it is detached from the session
            # that produced it, and a Decimal would imply a live row.
            format_money_short(Decimal(outcome.price), outcome.currency)
            if outcome.price is not None
            else "-",
            outcome.availability.value if outcome.availability else "unknown",
        )
    stdout.print(results)
    if any_failed:
        info("A shop that could not be read says nothing about whether it has stock.")


@entries_app.command("pause")
def pause_entry(
    entry_id: Annotated[int, typer.Argument()], user: UserOption = None
) -> None:
    """Stop scheduled checks for every retailer of this entry. History is kept."""
    _set_tracking(entry_id, user, active=False)


@entries_app.command("resume")
def resume_entry(
    entry_id: Annotated[int, typer.Argument()], user: UserOption = None
) -> None:
    """Resume scheduled checks for this entry."""
    _set_tracking(entry_id, user, active=True)


def _set_tracking(entry_id: int, user: str | None, *, active: bool) -> None:
    with session_scope() as session:
        owner = acting_user(session, user)
        try:
            _service(session, owner.id).set_tracking(entry_id, active=active)
        except NotFoundError as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.NOT_FOUND) from exc
    success(f"Product Entry {entry_id} {'resumed' if active else 'paused'}.")


@entries_app.command("remove")
def remove_entry(
    entry_id: Annotated[int, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation.")] = False,
    user: UserOption = None,
) -> None:
    """Archive an entry. Scheduled checks stop; every observation stays readable.

    Archived rather than deleted, because "I stopped following this" is a different fact
    from "this never happened", and the second one destroys evidence.
    """
    if not yes:
        typer.confirm(f"Archive product entry {entry_id}?", abort=True)
    with session_scope() as session:
        owner = acting_user(session, user)
        try:
            _service(session, owner.id).archive(entry_id)
        except NotFoundError as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.NOT_FOUND) from exc
    success(f"Product Entry {entry_id} archived. Its history is still there.")


@entries_app.command("remove-retailer")
def remove_retailer(
    entry_id: Annotated[int, typer.Argument()],
    retailer: Annotated[str, typer.Argument(help="Store slug, e.g. flipkart.")],
    yes: Annotated[bool, typer.Option("--yes")] = False,
    user: UserOption = None,
) -> None:
    """Drop one retailer from an entry, leaving the entry and the other shop intact."""
    if not yes:
        typer.confirm(f"Remove {_store_name(retailer)} from entry {entry_id}?", abort=True)
    with session_scope() as session:
        owner = acting_user(session, user)
        service = _service(session, owner.id)
        try:
            target = next(
                (
                    item
                    for item in service.active_listings(entry_id)
                    if item.store_slug == retailer
                ),
                None,
            )
            if target is None:
                error(f"this entry has no active {_store_name(retailer)} listing")
                raise typer.Exit(ExitCode.NOT_FOUND)
            service.deactivate_listing(entry_id, target.id)
        except NotFoundError as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.NOT_FOUND) from exc
    success(f"{_store_name(retailer)} removed from entry {entry_id}. Its history is kept.")
