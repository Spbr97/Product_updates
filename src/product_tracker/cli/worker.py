"""The ``worker`` command, and ``check --all``."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import typer

from ..core.config import get_settings
from ..db.session import session_scope
from ..domain.enums import CheckStatus
from ..repositories.products import ProductRepository
from ..scheduler.lock import WorkerAlreadyRunningError
from ..scheduler.runner import WorkerRunner, desired_schedule
from ..services.check_runner import deliver_pending, run_check
from ..stores.registry import default_registry
from ..utils.money import format_money
from .formatting import ExitCode, error, info, stdout, success, table, warn


def worker(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be scheduled, then exit."),
    ] = False,
) -> None:
    """Run the background worker: recurring checks on each product's interval.

    Runs until interrupted. Only one worker may run against a database at a time: an
    advisory lock makes a second one refuse to start, because two workers would each run
    every job and check every product twice.
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
    try:
        WorkerRunner(settings).run()
    except WorkerAlreadyRunningError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc
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

    with session_scope() as session:
        product_ids = [p.id for p in ProductRepository(session).list_schedulable()][:limit]

    if not product_ids:
        warn("no active products to check")
        return

    results = table(f"Checked {len(product_ids)} product(s)", ["Product", "Status", "Price"])
    failures = 0
    for product_id in product_ids:
        # Deliver once at the end rather than after each product, so one slow provider
        # does not stall the whole run.
        outcome = run_check(
            product_id, settings=settings, registry=default_registry(), deliver=False
        )
        if outcome.status is CheckStatus.FAILED:
            failures += 1
        results.add_row(
            str(product_id),
            _status_markup(outcome.status),
            format_money(Decimal(outcome.price) if outcome.price else None, outcome.currency),
        )

    stdout.print(results)
    report = deliver_pending(settings)
    if report.sent or report.failed:
        info(f"alerts: {report.sent} sent, {report.failed} failed")
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
