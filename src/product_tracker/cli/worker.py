"""The ``worker`` command, and ``check --all``."""

from __future__ import annotations

from typing import Annotated

import typer

from ..core.config import get_settings
from ..db.session import session_scope
from ..domain.enums import CheckStatus
from ..repositories.products import ProductRepository
from ..scheduler.runner import WorkerRunner, desired_schedule
from ..services.tracking import TrackingEngine
from ..stores.registry import default_registry
from ..utils.money import format_money
from .formatting import ExitCode, info, stdout, success, table, warn


def worker(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be scheduled, then exit."),
    ] = False,
) -> None:
    """Run the background worker: recurring checks on each product's interval.

    Runs until interrupted. Only one worker should run against a database at a time --
    APScheduler's job store has no cross-process locking, so two workers would each run
    every job.
    """
    settings = get_settings()

    if dry_run:
        schedule = desired_schedule(settings)
        if not schedule:
            warn("no active products to schedule")
            raise typer.Exit(ExitCode.OK)
        listing = table(
            f"Would schedule {len(schedule)} product(s)", ["Product", "Every"]
        )
        for product_id, interval in sorted(schedule.items()):
            listing.add_row(str(product_id), _humanise(interval))
        stdout.print(listing)
        raise typer.Exit(ExitCode.OK)

    info("Starting worker. Press Ctrl+C to stop.")
    WorkerRunner(settings).run()
    success("worker stopped")


def check_all(
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=1000, help="Maximum products to check.")
    ] = 100,
) -> None:
    """Check every active product once, now.

    Sequential and unthrottled -- this is a manual operation, not the scheduler. For
    ongoing checking use ``product-tracker worker``.
    """
    settings = get_settings()
    engine = TrackingEngine(default_registry(), settings)

    with session_scope() as session:
        product_ids = [p.id for p in ProductRepository(session).list_schedulable()][:limit]

    if not product_ids:
        warn("no active products to check")
        return

    results = table(f"Checked {len(product_ids)} product(s)", ["Product", "Status", "Price"])
    failures = 0
    for product_id in product_ids:
        with session_scope() as session:
            execution = engine.check_product(session, product_id)
            status = execution.status
            price = format_money(execution.extracted_price, execution.extracted_currency)
        if status is CheckStatus.FAILED:
            failures += 1
        results.add_row(str(product_id), _status_markup(status), price)

    stdout.print(results)
    if failures:
        warn(f"{failures} of {len(product_ids)} checks failed")
        raise typer.Exit(ExitCode.STORE_FAILURE)


def _humanise(seconds: int) -> str:
    if seconds % 86_400 == 0:
        return f"{seconds // 86_400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _status_markup(status: CheckStatus) -> str:
    colour = {
        CheckStatus.SUCCESS: "green",
        CheckStatus.PARTIAL: "yellow",
        CheckStatus.FAILED: "red",
        CheckStatus.SKIPPED: "dim",
    }[status]
    return f"[{colour}]{status.value}[/{colour}]"
