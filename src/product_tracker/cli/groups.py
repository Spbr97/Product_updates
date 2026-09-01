"""Group commands, and the comparison grid.

``compare`` is the command this whole feature exists for: one product, its models down the
side, the shops across the top. Everything else here is the bookkeeping that makes it
possible.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

import typer
from rich import box
from rich.table import Table

from ..db.session import session_scope
from ..domain.enums import CellStatus
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..domain.models import ComparisonCell, ComparisonMatrix
from ..repositories.groups import GroupRepository
from ..services import group_service
from ..services.comparison import DEFAULT_STALE_AFTER, GroupNotFoundError, build_matrix
from ..utils.money import format_money_short
from .formatting import ExitCode, error, info, stdout, success, table
from .users import UserOption, acting_user

groups_app = typer.Typer(help="Group listings by product and model.", no_args_is_help=True)

#: How each non-price cell reads, and the colour it reads in. The wording matters as much
#: as the data: "blocked" and "sold out" must never look like the same thing.
_CELL_TEXT: dict[CellStatus, tuple[str, str]] = {
    CellStatus.OUT_OF_STOCK: ("sold out", "red"),
    CellStatus.NO_PRICE: ("no price", "yellow"),
    CellStatus.BLOCKED: ("blocked", "magenta"),
    CellStatus.FAILED: ("failed", "red"),
    CellStatus.NEVER_CHECKED: ("pending", "dim"),
    CellStatus.NOT_TRACKED: ("-", "dim"),
}

_LEGEND: dict[CellStatus, str] = {
    CellStatus.OUT_OF_STOCK: "the shop lists it as sold out",
    CellStatus.NO_PRICE: "the page loaded but no price could be read",
    CellStatus.BLOCKED: "the shop refused our request - this says nothing about stock",
    CellStatus.FAILED: "the check failed (timeout, error, or unexpected page)",
    CellStatus.NEVER_CHECKED: "tracked, but not checked yet",
    CellStatus.NOT_TRACKED: "no listing tracked at this shop",
}


def _render_cell(cell: ComparisonCell) -> str:
    """One square of the grid, as markup.

    A sold-out cell deliberately shows no price. We still hold the last one, but this is a
    grid about what a person can buy and for how much, and a price beside something
    unbuyable reads as an offer. ``show`` and ``history`` have the number if it is wanted.
    """
    if cell.status is not CellStatus.OK:
        text, colour = _CELL_TEXT[cell.status]
        return f"[{colour}]{text}[/{colour}]"

    rendered = format_money_short(cell.price, cell.currency)
    delta = cell.price_delta
    if delta is not None and delta != 0:
        arrow, colour = ("v", "green") if delta < 0 else ("^", "red")
        moved = format_money_short(abs(delta), cell.currency)
        rendered += f" [{colour}]{arrow}{moved}[/{colour}]"
    if cell.is_stale:
        rendered += " [dim]*[/dim]"
    return rendered


def _grid(matrix: ComparisonMatrix) -> Table:
    """The grid itself.

    Built directly rather than through the shared ``table`` helper: that one folds long
    cells onto extra lines, which turns a comparison of five shops into an unreadable
    stack. Here every cell is short by construction, so nothing needs to wrap.
    """
    heading = matrix.group_name + (f"  ·  {matrix.brand}" if matrix.brand else "")
    grid = Table(
        title=heading,
        title_justify="left",
        header_style="bold",
        box=box.SIMPLE_HEAD,
        padding=(0, 1),
    )
    grid.add_column("Model", no_wrap=True, style="bold")
    for slug in matrix.store_slugs:
        grid.add_column(matrix.store_names[slug], justify="right", no_wrap=True)

    for row in matrix.rows:
        best = set(row.best_store_slugs)
        cells: list[str] = []
        for slug in matrix.store_slugs:
            rendered = _render_cell(row.cells[slug])
            # Highlight the cheapest, but only when there is a real choice to point at.
            if slug in best and len(best) < len(matrix.store_slugs):
                rendered = f"[bold green]{rendered}[/bold green]"
            cells.append(rendered)
        grid.add_row(row.label, *cells)
    return grid


def _stacked(matrix: ComparisonMatrix) -> None:
    """One block per model, shops listed underneath.

    The fallback when the grid will not fit the terminal. Squeezing five shops into eighty
    columns truncates every heading to "Reliance Dig..." and every price to a fragment,
    which is worse than not using a grid at all -- so at that point we stop trying.
    """
    width = max((len(matrix.store_names[s]) for s in matrix.store_slugs), default=0)
    for row in matrix.rows:
        best = set(row.best_store_slugs)
        stdout.print(f"[bold]{row.label}[/bold]")
        # Priced shops first, cheapest first; the rest keep their column order.
        priced = [s for s in matrix.store_slugs if row.cells[s].has_price]
        priced.sort(key=lambda s: row.cells[s].price or 0)
        for slug in [*priced, *(s for s in matrix.store_slugs if s not in priced)]:
            rendered = _render_cell(row.cells[slug])
            marker = "  [green]<- best[/green]" if slug in best and len(best) < len(
                matrix.store_slugs
            ) else ""
            stdout.print(f"  {matrix.store_names[slug]:<{width}}  {rendered}{marker}")
        stdout.print()


def _summary(matrix: ComparisonMatrix) -> None:
    if matrix.mixed_currency:
        info(
            "Listings report different currencies "
            f"({', '.join(matrix.currencies)}); prices are never converted, "
            "so there is no single best price."
        )
        return

    best = matrix.best_overall
    if best is None:
        info("No comparable price yet.")
        return

    currency = matrix.currencies[0] if matrix.currencies else None
    row, store_slug = best
    price = format_money_short(row.best_price, currency)
    line = f"Best: {price} - {row.label} at {matrix.store_names[store_slug]}"
    spread = row.spread
    if spread is not None and spread > 0:
        line += f"  (saves {format_money_short(spread, currency)} vs the dearest)"
    success(line)


def _legend(matrix: ComparisonMatrix) -> None:
    """Explain only the markers actually on screen."""
    used = {
        cell.status
        for row in matrix.rows
        for cell in row.cells.values()
        if cell.status is not CellStatus.OK
    }
    for status, (label, _colour) in _CELL_TEXT.items():
        if status in used:
            stdout.print(f"  [dim]{label:<9} {_LEGEND[status]}[/dim]")
    if any(cell.is_stale for row in matrix.rows for cell in row.cells.values()):
        stdout.print("  [dim]*         not checked recently[/dim]")


def compare(
    slug: Annotated[str, typer.Argument(help="Group slug, e.g. iphone-17.")],
    stale_hours: Annotated[
        int, typer.Option("--stale-hours", min=1, help="Flag prices older than this.")
    ] = int(DEFAULT_STALE_AFTER.total_seconds() // 3600),
    layout: Annotated[
        str,
        typer.Option("--layout", help="grid, list, or auto (grid when it fits)."),
    ] = "auto",
    user: UserOption = None,
) -> None:
    """Compare one product across its models and every shop that sells it."""
    if layout not in {"auto", "grid", "list"}:
        error(f"unknown layout {layout!r}: choose grid, list, or auto")
        raise typer.Exit(ExitCode.ERROR)

    with session_scope() as session:
        try:
            matrix = build_matrix(
                session,
                slug,
                user_id=acting_user(session, user).id,
                stale_after=timedelta(hours=stale_hours),
            )
        except GroupNotFoundError as exc:
            error(str(exc))
            raise typer.Exit(ExitCode.NOT_FOUND) from exc

        if not matrix.rows:
            info(f"{matrix.group_name} has no models yet. Attach a listing with:")
            stdout.print(
                f"  product-tracker groups attach <product-id> --group {matrix.group_slug}"
            )
            return

        grid = _grid(matrix)
        # Ask rich how wide the table really wants to be rather than estimating it: a
        # guess that is a few columns short silently truncates every heading, which is the
        # exact failure the stacked layout exists to avoid.
        # measure() clamps to the console width, which would make every table appear
        # to fit; ask for its natural width instead.
        natural = stdout.measure(grid, options=stdout.options.update(max_width=10_000))
        fits = natural.maximum <= stdout.width
        as_grid = layout == "grid" or (layout == "auto" and fits)

        stdout.print()
        if as_grid:
            stdout.print(grid)
            stdout.print()
        else:
            heading = matrix.group_name + (f"  ·  {matrix.brand}" if matrix.brand else "")
            stdout.print(f"[bold]{heading}[/bold]")
            stdout.print()
            _stacked(matrix)
        _summary(matrix)
        _legend(matrix)
        stdout.print()


def list_groups(user: UserOption = None) -> None:
    """List product groups."""
    with session_scope() as session:
        owner = acting_user(session, user)
        repo = GroupRepository(session)
        groups = repo.list_all(owner.id)
        counts = repo.listing_counts(owner.id)

        if not groups:
            info("No product groups yet. Create one with: product-tracker groups add <name>")
            return

        listing = table("Product groups", ["Slug", "Name", "Brand", "Models", "Listings"])
        for group in groups:
            listing.add_row(
                group.slug,
                group.name,
                group.brand or "-",
                str(len(group.variants)),
                str(counts.get(group.id, 0)),
            )
        stdout.print(listing)


def add_group(
    name: Annotated[str, typer.Argument(help="Display name, e.g. 'iPhone 17'.")],
    slug: Annotated[
        str | None, typer.Option("--slug", help="URL-safe id. Derived from the name if omitted.")
    ] = None,
    brand: Annotated[str | None, typer.Option("--brand", help="Manufacturer.")] = None,
    user: UserOption = None,
) -> None:
    """Create a product group."""
    try:
        with session_scope() as session:
            group = group_service.create_group(
                session,
                user_id=acting_user(session, user).id,
                slug=slug,
                name=name,
                brand=brand,
            )
            created = group.slug
    except ValidationError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc
    except DuplicateError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc
    success(f"created group {created}")


def remove_group(
    slug: Annotated[str, typer.Argument(help="Group slug.")],
    user: UserOption = None,
) -> None:
    """Delete a group. Tracked listings and their price history are kept."""
    try:
        with session_scope() as session:
            group_service.delete_group(session, acting_user(session, user).id, slug)
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc
    success(f"removed group {slug} (listings and history kept)")


def attach(
    product_id: Annotated[int, typer.Argument(help="Tracked product id.")],
    group: Annotated[str, typer.Option("--group", help="Group slug.")],
    variant: Annotated[
        str | None,
        typer.Option("--variant", help="Model label, e.g. '256GB / Lavender'. Inferred if unset."),
    ] = None,
    user: UserOption = None,
) -> None:
    """Attach a tracked listing to the model it sells."""
    try:
        with session_scope() as session:
            product, resolved = group_service.attach_product(
                session,
                product_id,
                user_id=acting_user(session, user).id,
                group_slug=group,
                label=variant,
            )
            label, store = resolved.label, product.store.name
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc
    except ValidationError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc
    success(f"product {product_id} ({store}) is now {group} / {label}")


def detach(
    product_id: Annotated[int, typer.Argument(help="Tracked product id.")],
    user: UserOption = None,
) -> None:
    """Remove a listing from its group. Tracking and history are unaffected."""
    try:
        with session_scope() as session:
            group_service.detach_product(session, product_id, acting_user(session, user).id)
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc
    success(f"product {product_id} detached (still tracked)")


groups_app.command("list")(list_groups)
groups_app.command("add")(add_group)
groups_app.command("remove")(remove_group)
groups_app.command("attach")(attach)
groups_app.command("detach")(detach)
