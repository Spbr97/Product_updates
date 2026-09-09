"""``product-tracker init``: apply a declarative tracking file."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ..core.config import get_settings
from ..db.session import session_scope
from ..domain.errors import ValidationError
from ..services import provisioning
from ..stores.registry import default_registry
from .formatting import ExitCode, error, stdout, success, table, warn
from .users import UserOption, acting_user

DEFAULT_FILE = Path("tracking.yaml")
EXAMPLE_FILE = Path("tracking.example.yaml")


def init(
    file: Annotated[
        Path,
        typer.Option("--file", "-f", help="The tracking file to apply."),
    ] = DEFAULT_FILE,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would happen without writing anything."),
    ] = False,
    user: UserOption = None,
) -> None:
    """Track everything a tracking file declares.

    Idempotent: a listing you already watch is reported as such rather than added twice,
    so this is safe to re-run after a rebuild or on a second machine.
    """
    if not file.exists():
        error(f"no {file}")
        if EXAMPLE_FILE.exists():
            stdout.print(
                f"  Start from the example:  Copy-Item {EXAMPLE_FILE} {DEFAULT_FILE}"
            )
        raise typer.Exit(ExitCode.ERROR)

    try:
        plan = provisioning.load(file)
    except ValidationError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc

    if dry_run:
        _preview(plan, file)
        return

    try:
        with session_scope() as session:
            report = provisioning.apply(
                session,
                plan,
                user_id=acting_user(session, user).id,
                settings=get_settings(),
                registry=default_registry(),
            )
            rows = [
                (
                    product.name,
                    product.group_slug or "[dim]-[/dim]",
                    listing.status.replace("_", " "),
                    listing.url,
                    listing.detail or "",
                )
                for product in report.products
                for listing in product.listings
            ]
            counts = (report.tracked, report.already, report.failed)
            alerts = sum(product.alerts_added for product in report.products)
            advice = report.pincode_advice
    except ValidationError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc

    listing_table = table(
        f"Applied {file}", ["Product", "Group", "Result", "URL", "Detail"]
    )
    for row in rows:
        listing_table.add_row(*row)
    stdout.print(listing_table)

    tracked, already, failed = counts
    success(
        f"{tracked} newly tracked, {already} already tracked, {failed} failed"
        + (f"; {alerts} alert rules added" if alerts else "")
    )
    if advice:
        warn(advice)
    if failed:
        raise typer.Exit(ExitCode.ERROR)


def _preview(plan: provisioning.Plan, file: Path) -> None:
    """What the file asks for, without touching the database."""
    preview = table(f"{file} (dry run)", ["Product", "Group", "Listings", "Alerts"])
    for entry in plan.products:
        preview.add_row(
            str(entry["name"]),
            str(entry.get("group") or "-"),
            str(len(entry["listings"])),
            ", ".join(str(a["type"]) for a in entry.get("alerts") or []) or "-",
        )
    stdout.print(preview)
    if plan.delivery_pincode:
        current = get_settings().delivery_pincode
        if current != plan.delivery_pincode:
            warn(
                f"declares delivery area {plan.delivery_pincode}; "
                f"DELIVERY_PINCODE is currently {current or 'unset'}"
            )
    success("nothing was written")
