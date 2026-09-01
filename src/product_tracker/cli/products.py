"""Product commands: add, list, show, remove, check."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import typer

from ..core.config import get_settings
from ..db.session import session_scope
from ..domain.enums import CheckStatus, TrackingStatus
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..repositories.executions import CheckExecutionRepository
from ..services.check_runner import run_check
from ..services.product_service import ProductService
from ..stores.registry import default_registry
from ..utils.money import format_money
from .formatting import ExitCode, error, info, stdout, success, table, warn
from .users import UserOption, acting_user


def _service(session, user: str | None = None) -> ProductService:  # type: ignore[no-untyped-def]
    return ProductService(
        session, default_registry(), get_settings(), acting_user(session, user).id
    )


def add(
    url: Annotated[str, typer.Argument(help="Product page URL.")],
    interval: Annotated[
        int | None,
        typer.Option("--interval", min=60, help="Seconds between checks for this product."),
    ] = None,
    check_now: Annotated[
        bool, typer.Option("--check/--no-check", help="Run a check immediately after adding.")
    ] = True,
    user: UserOption = None,
) -> None:
    """Track a new product by URL."""
    try:
        with session_scope() as session:
            product = _service(session, user).add(url, check_interval_seconds=interval)
            product_id = product.id
            store_name = product.store.name
    except ValidationError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc
    except DuplicateError as exc:
        error(f"already tracked: {exc.identifier}")
        raise typer.Exit(ExitCode.ERROR) from exc
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc

    success(f"tracking product {product_id} via {store_name}")
    if check_now:
        info("")
        check(product_id)


def list_products(
    store: Annotated[str | None, typer.Option("--store", help="Filter by store slug.")] = None,
    status: Annotated[
        TrackingStatus | None, typer.Option("--status", help="Filter by tracking status.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 20,
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
    user: UserOption = None,
) -> None:
    """List tracked products."""
    with session_scope() as session:
        page = _service(session, user).list(
            limit=limit, offset=offset, store_slug=store, tracking_status=status
        )
        rows = [
            (
                str(p.id),
                p.store.slug,
                (p.name or "[dim]not yet checked[/dim]")[:58],
                format_money(p.current_price, p.currency),
                p.availability.value,
                p.tracking_status.value,
            )
            for p in page.items
        ]
        total = page.total

    if not rows:
        warn("no products tracked yet -- add one with: product-tracker add <URL>")
        return

    listing = table(
        f"Products ({offset + 1}-{offset + len(rows)} of {total})",
        ["ID", "Store", "Name", "Price", "Availability", "Tracking"],
    )
    for row in rows:
        listing.add_row(*row)
    stdout.print(listing)


def show(
    product_id: Annotated[int, typer.Argument(help="Product ID.")],
    user: UserOption = None,
) -> None:
    """Show one product in detail, with its most recent checks."""
    try:
        with session_scope() as session:
            product = _service(session, user).get(product_id)
            detail = table(f"Product {product.id}", ["Field", "Value"])
            detail.add_row("name", product.name or "-")
            detail.add_row("store", product.store.name)
            detail.add_row("url", product.url)
            detail.add_row("price", format_money(product.current_price, product.currency))
            detail.add_row("availability", product.availability.value)
            detail.add_row("tracking", product.tracking_status.value)
            detail.add_row("identifier", product.product_identifier or "-")
            detail.add_row(
                "interval",
                f"{product.check_interval_seconds}s"
                if product.check_interval_seconds
                else "default",
            )
            detail.add_row(
                "last checked",
                product.last_checked_at.strftime("%Y-%m-%d %H:%M UTC")
                if product.last_checked_at
                else "never",
            )
            detail.add_row("consecutive failures", str(product.consecutive_failures))

            executions = CheckExecutionRepository(session).list_for_product(product_id, limit=5)
            history = table("Recent checks", ["When", "Status", "Method", "Price", "Detail"])
            for execution in executions:
                history.add_row(
                    execution.started_at.strftime("%Y-%m-%d %H:%M"),
                    _status_markup(execution.status),
                    execution.fetch_method.value,
                    format_money(execution.extracted_price, execution.extracted_currency),
                    (execution.error_detail or "")[:60],
                )
            has_history = bool(executions)
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc

    stdout.print(detail)
    if has_history:
        stdout.print(history)


def remove(
    product_id: Annotated[int, typer.Argument(help="Product ID.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
    user: UserOption = None,
) -> None:
    """Stop watching a product. It is deleted once nobody watches it."""
    try:
        with session_scope() as session:
            product = _service(session, user).get(product_id)
            label = product.name or product.url
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc

    if not yes:
        # Deleting a product cascades to its price history, which cannot be recovered.
        typer.confirm(
            f"Delete product {product_id} ({label[:60]}) and all its history?", abort=True
        )

    with session_scope() as session:
        _service(session, user).remove(product_id)
    success(f"removed product {product_id}")


def check(product_id: Annotated[int, typer.Argument(help="Product ID.")]) -> None:
    """Check one product now and report what was found."""
    try:
        # Owns both transactions: the check, then delivery of anything it produced.
        outcome = run_check(product_id, settings=get_settings(), registry=default_registry())
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc

    status = outcome.status
    result = table(f"Check result for product {product_id}", ["Field", "Value"])
    result.add_row("status", _status_markup(status))
    result.add_row(
        "price",
        format_money(Decimal(outcome.price) if outcome.price else None, outcome.currency),
    )
    result.add_row(
        "availability", outcome.availability.value if outcome.availability else "unknown"
    )
    result.add_row("method", outcome.fetch_method.value)
    result.add_row("attempts", str(outcome.attempts))
    result.add_row(
        "duration", f"{outcome.duration_ms} ms" if outcome.duration_ms is not None else "-"
    )
    if outcome.notifications_created:
        result.add_row(
            "alerts", f"{outcome.notifications_sent} sent of {outcome.notifications_created}"
        )
    stdout.print(result)

    if outcome.error_detail:
        warn(outcome.error_detail)

    if status is CheckStatus.FAILED:
        # A store we could not read is a distinct condition from a crash: scripts branch
        # on it to decide whether to retry later or investigate.
        raise typer.Exit(ExitCode.STORE_FAILURE)


def _status_markup(status: CheckStatus) -> str:
    colour = {
        CheckStatus.SUCCESS: "green",
        CheckStatus.PARTIAL: "yellow",
        CheckStatus.FAILED: "red",
        CheckStatus.SKIPPED: "dim",
    }[status]
    return f"[{colour}]{status.value}[/{colour}]"
