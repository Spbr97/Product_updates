"""The ``worker`` command, and ``check --all``."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import typer

from ..core.config import get_settings
from ..domain.enums import CheckStatus
from ..scheduler.lock import WorkerAlreadyRunningError
from ..scheduler.runner import WorkerRunner, desired_schedule
from ..services.check_runner import run_all_checks
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

    Each product is claimed first, for the same reason the scheduler claims: two of these
    running at once, or one running beside a worker, would otherwise check every product
    twice and hit every shop twice as hard. A product already being checked is skipped and
    reported as such, which is honest -- the check is happening, just not here.
    """
    settings = get_settings()

    summary = run_all_checks(settings, limit=limit)

    if not summary.outcomes and not summary.skipped:
        warn("no active products to check")
        return

    results = table(f"Checked {summary.checked} product(s)", ["Product", "Status", "Price"])
    for outcome in summary.outcomes:
        results.add_row(
            str(outcome.product_id),
            _status_markup(outcome.status),
            format_money(Decimal(outcome.price) if outcome.price else None, outcome.currency),
        )

    stdout.print(results)
    if summary.skipped:
        info(f"{summary.skipped} product(s) were already being checked elsewhere, and were skipped")
    if summary.delivery.sent or summary.delivery.failed:
        info(f"alerts: {summary.delivery.sent} sent, {summary.delivery.failed} failed")
    if summary.failed:
        warn(f"{summary.failed} of {summary.checked} checks failed")
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
