"""The ``history`` command."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

import typer

from ..db.session import session_scope
from ..domain.errors import NotFoundError
from ..domain.models import PriceStats
from ..services.history_service import HistoryService
from ..utils.money import format_money
from .formatting import ExitCode, error, stdout, table, warn


def history(
    product_id: Annotated[int, typer.Argument(help="Product ID.")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 20,
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
    stats: Annotated[
        bool, typer.Option("--stats", help="Show price statistics instead of the raw series.")
    ] = False,
    availability: Annotated[
        bool, typer.Option("--availability", help="Show availability history instead of prices.")
    ] = False,
) -> None:
    """Show recorded price history for a product."""
    try:
        with session_scope() as session:
            service = HistoryService(session)
            if stats:
                _render_stats(service.stats(product_id), product_id)
                return
            if availability:
                _render_availability(service, product_id, limit, offset)
                return
            _render_prices(service, product_id, limit, offset)
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc


def _render_prices(
    service: HistoryService, product_id: int, limit: int, offset: int
) -> None:
    page = service.price_history(product_id, limit=limit, offset=offset)
    if not page.items:
        warn("no price history yet -- run: product-tracker check <ID>")
        return

    listing = table(
        f"Price history for product {product_id} "
        f"({offset + 1}-{offset + len(page.items)} of {page.total})",
        ["Observed (UTC)", "Price", "Change"],
    )
    # Rows arrive newest first; the "change" column compares each row to the one before
    # it in time, which is the next row in this ordering.
    for index, entry in enumerate(page.items):
        older = page.items[index + 1] if index + 1 < len(page.items) else None
        listing.add_row(
            entry.observed_at.strftime("%Y-%m-%d %H:%M"),
            format_money(entry.price, entry.currency),
            _delta(entry.price, older.price if older else None, entry.currency),
        )
    stdout.print(listing)


def _render_availability(
    service: HistoryService, product_id: int, limit: int, offset: int
) -> None:
    page = service.availability_history(product_id, limit=limit, offset=offset)
    if not page.items:
        warn("no availability history yet -- run: product-tracker check <ID>")
        return

    listing = table(
        f"Availability history for product {product_id} "
        f"({offset + 1}-{offset + len(page.items)} of {page.total})",
        ["Observed (UTC)", "Availability"],
    )
    for entry in page.items:
        listing.add_row(
            entry.observed_at.strftime("%Y-%m-%d %H:%M"),
            _availability_markup(entry.availability.value),
        )
    stdout.print(listing)


def _render_stats(stats: PriceStats | None, product_id: int) -> None:
    if stats is None:
        warn("no price history yet -- run: product-tracker check <ID>")
        return

    listing = table(f"Price statistics for product {product_id}", ["Metric", "Value"])
    listing.add_row("observations", str(stats.observations))
    listing.add_row("currency", stats.currency)
    listing.add_row("current", format_money(stats.current, stats.currency))
    listing.add_row(
        "lowest",
        f"{format_money(stats.lowest, stats.currency)}{_at(stats.lowest_at)}",
    )
    listing.add_row(
        "highest",
        f"{format_money(stats.highest, stats.currency)}{_at(stats.highest_at)}",
    )
    listing.add_row("average", format_money(stats.average, stats.currency))
    listing.add_row("first observed", _at(stats.first_observed_at).strip(" ()") or "-")
    listing.add_row(
        "change since first",
        _delta(stats.current, _first_price(stats), stats.currency),
    )
    if stats.changed_pct is not None:
        listing.add_row("change %", f"{stats.changed_pct:+.2f}%")
    stdout.print(listing)

    if stats.mixed_currency:
        warn(
            f"this product has been priced in more than one currency; statistics cover "
            f"{stats.currency} only"
        )


def _first_price(stats: PriceStats) -> Decimal | None:
    """The first observed price, recovered from current minus the recorded change."""
    if stats.current is None or stats.changed_by is None:
        return None
    return stats.current - stats.changed_by


def _at(value: datetime | None) -> str:
    return f" ({value:%Y-%m-%d %H:%M})" if value is not None else ""


def _delta(current: Decimal | None, previous: Decimal | None, currency: str | None) -> str:
    """A signed, coloured price delta, or a dash when there is nothing to compare.

    Green for a drop, red for a rise -- the tracker exists to catch drops, so they should
    read as the good news.
    """
    if current is None or previous is None:
        return "[dim]-[/dim]"
    difference = current - previous
    if difference == 0:
        return "[dim]no change[/dim]"
    colour = "green" if difference < 0 else "red"
    sign = "-" if difference < 0 else "+"
    return f"[{colour}]{sign}{format_money(abs(difference), currency)}[/{colour}]"


def _availability_markup(value: str) -> str:
    colour = {
        "in_stock": "green",
        "out_of_stock": "red",
        "unavailable": "red",
        "unknown": "dim",
    }.get(value, "white")
    return f"[{colour}]{value}[/{colour}]"
