"""System commands: status, stores, config.

These are the commands that let an operator answer "is this thing set up correctly?"
without reaching for psql.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import typer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__
from ..core.config import Settings, get_settings, mask_dsn_password
from ..db.models import CheckExecution
from ..db.session import current_revision, get_session_factory, ping
from ..domain.enums import TrackingStatus
from ..domain.errors import ConfigurationError
from ..notifications.registry import provider_status
from ..repositories.products import ProductRepository
from ..repositories.stores import StoreRepository
from ..scheduler import heartbeat
from ..scheduler.status import scheduler_status
from ..stores.catalogue import KNOWN_STORES
from .formatting import ExitCode, error, info, stdout, success, table, warn, yes_no

stores_app = typer.Typer(help="Inspect and sync supported stores.", no_args_is_help=True)


def status() -> None:
    """Report configuration, database, and tracking state."""
    stdout.print(f"[bold]product-tracker[/bold] {__version__}\n")

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        error(str(exc))
        info("\nSet DATABASE_URL in your environment or .env (see .env.example).")
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc

    summary = table("Configuration", ["Setting", "Value"])
    summary.add_row("database", mask_dsn_password(settings.database_url))
    summary.add_row("log level", settings.log_level)
    summary.add_row("check interval", f"{settings.check_interval_seconds}s")
    summary.add_row("playwright", yes_no(settings.playwright_enabled))
    summary.add_row("api key set", yes_no(settings.api_key is not None))
    summary.add_row("providers", ", ".join(settings.notification_providers) or "-")
    stdout.print(summary)

    session = get_session_factory()()
    try:
        if not ping(session):
            error("database unreachable")
            raise typer.Exit(ExitCode.CONFIG_ERROR)

        revision = current_revision(session)
        if revision is None:
            warn("database reachable but not migrated -- run: alembic upgrade head")
            raise typer.Exit(ExitCode.CONFIG_ERROR)

        counts = ProductRepository(session).count_by_status()
        store_count = StoreRepository(session).count()

        state = table("State", ["Item", "Value"])
        state.add_row("migration revision", revision)
        state.add_row("stores registered", str(store_count))
        state.add_row("products active", str(counts[TrackingStatus.ACTIVE]))
        state.add_row("products paused", str(counts[TrackingStatus.PAUSED]))
        stdout.print(state)

        _print_worker(session, settings)
        _print_recent_checks(session)
        _print_providers(settings)

        if store_count == 0:
            warn("no stores registered -- run: product-tracker stores sync")
    finally:
        session.close()


def _print_worker(session: Session, settings: Settings) -> None:
    """What work is scheduled, and whether a worker is alive.

    Two different questions with two different sources: the job store says what is
    scheduled; the heartbeat says whether anything is running it.
    """
    state = scheduler_status(session)
    liveness = heartbeat.read(
        session, reconcile_interval_seconds=settings.reconcile_interval_seconds
    )

    worker = table("Worker", ["Item", "Value"])
    worker.add_row("job store", "available" if state.available else "[red]missing[/red]")
    worker.add_row("product jobs", str(state.product_jobs))
    worker.add_row(
        "next run",
        state.next_run_at.strftime("%Y-%m-%d %H:%M UTC") if state.next_run_at else "-",
    )
    running = liveness.running
    worker.add_row(
        "worker",
        "[green]running[/green]"
        if running
        else "[dim]never started[/dim]"
        if running is None
        else "[red]not running[/red]",
    )
    worker.add_row("heartbeat", liveness.detail)
    stdout.print(worker)

    if running is False:
        warn("no worker is running -- start it with: product-tracker worker")
    if liveness.workers > 1:
        warn(
            f"{liveness.workers} workers are reporting in; run only one per database, "
            "or every job runs more than once"
        )


def _print_recent_checks(session: Session) -> None:
    """A day's worth of check outcomes: the quickest read on whether tracking is healthy."""
    since = datetime.now(UTC) - timedelta(days=1)
    rows = session.execute(
        select(CheckExecution.status, func.count())
        .where(CheckExecution.started_at >= since)
        .group_by(CheckExecution.status)
    ).all()

    if not rows:
        return

    summary = table("Checks (last 24h)", ["Status", "Count"])
    for status_value, count in sorted(rows, key=lambda row: row[0].value):
        summary.add_row(status_value.value, str(count))
    stdout.print(summary)


def _print_providers(settings: Settings) -> None:
    providers = table("Notifications", ["Provider", "Enabled", "Configured"])
    for slug, _name, enabled, configured in provider_status(settings):
        providers.add_row(slug, yes_no(enabled), yes_no(configured))
    stdout.print(providers)

    usable = [
        slug for slug, _n, enabled, configured in provider_status(settings)
        if enabled and configured
    ]
    if not usable:
        warn("no usable notification provider -- alerts will be recorded but not delivered")


@stores_app.command("list")
def stores_list() -> None:
    """List stores registered in the database."""
    session = get_session_factory()()
    try:
        rows = StoreRepository(session).list_all()
    finally:
        session.close()

    if not rows:
        warn("no stores registered -- run: product-tracker stores sync")
        raise typer.Exit(ExitCode.OK)

    listing = table("Stores", ["Slug", "Name", "Adapter", "Enabled", "Domains"])
    for store in rows:
        listing.add_row(
            store.slug,
            store.name,
            store.adapter_key,
            yes_no(store.enabled),
            ", ".join(store.domains) or "[dim]any (fallback)[/dim]",
        )
    stdout.print(listing)


@stores_app.command("sync")
def stores_sync() -> None:
    """Reconcile the stores table with the adapters compiled into this build.

    Stores are never deleted -- products may still reference them. Disable instead.
    """
    session = get_session_factory()()
    try:
        created, updated = StoreRepository(session).sync_from_registry(list(KNOWN_STORES))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    success(f"stores synced: {created} created, {updated} updated")


def config_show() -> None:
    """Print effective settings with every secret redacted."""
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.CONFIG_ERROR) from exc

    listing = table("Effective configuration", ["Setting", "Value"])
    for key, value in sorted(settings.redacted().items()):
        listing.add_row(key, "-" if value is None else str(value))
    stdout.print(listing)
