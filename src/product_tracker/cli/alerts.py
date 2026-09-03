"""Alert commands: ``alerts list|add|remove``, plus ``pause`` and ``resume``."""

from __future__ import annotations

from typing import Annotated

import typer

from ..db.session import session_scope
from ..domain.enums import RuleType, TrackingStatus
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..notifications.registry import ALL_PROVIDERS
from ..repositories.notifications import NotificationRepository
from ..services.alert_service import AlertService
from .formatting import ExitCode, error, stdout, success, table, warn, yes_no
from .users import UserOption, acting_user

alerts_app = typer.Typer(help="Manage tracking rules (alerts).", no_args_is_help=True)


@alerts_app.command("add")
def alerts_add(
    product_id: Annotated[int, typer.Argument(help="Product to alert on.")],
    rule_type: Annotated[
        RuleType, typer.Option("--type", "-t", help="Condition to watch for.")
    ],
    target: Annotated[
        float | None,
        typer.Option("--target", help="Target price. Required for price_below_target."),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help=f"Send via one channel ({', '.join(ALL_PROVIDERS)})."),
    ] = None,
    cooldown: Annotated[
        int | None,
        typer.Option("--cooldown", min=0, help="Minimum seconds between firings."),
    ] = None,
    user: UserOption = None,
) -> None:
    """Add a tracking rule to a product."""
    params: dict[str, object] = {}
    if target is not None:
        params["target_price"] = str(target)

    try:
        with session_scope() as session:
            rule = AlertService(session, acting_user(session, user).id).add(
                product_id,
                rule_type,
                params=params,
                notify_provider=provider,
                cooldown_seconds=cooldown,
            )
            rule_id = rule.id
    except ValidationError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc
    except DuplicateError as exc:
        error(f"already exists: {exc.identifier}")
        raise typer.Exit(ExitCode.ERROR) from exc
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc

    success(f"alert {rule_id} added: {rule_type.value} on product {product_id}")


@alerts_app.command("list")
def alerts_list(
    product_id: Annotated[
        int | None, typer.Option("--product", help="Only this product's alerts.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 50,
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
    user: UserOption = None,
) -> None:
    """List tracking rules."""
    try:
        with session_scope() as session:
            page = AlertService(session, acting_user(session, user).id).list(
                product_id=product_id, limit=limit, offset=offset
            )
            rows = [
                (
                    str(rule.id),
                    str(rule.product_id),
                    rule.rule_type.value,
                    _params_summary(rule.params),
                    rule.notify_provider or "[dim]all[/dim]",
                    f"{rule.cooldown_seconds}s" if rule.cooldown_seconds else "-",
                    yes_no(rule.enabled),
                    rule.last_fired_at.strftime("%Y-%m-%d %H:%M") if rule.last_fired_at else "-",
                )
                for rule in page.items
            ]
            total = page.total
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc

    if not rows:
        warn("no alerts configured -- add one with: product-tracker alerts add <ID> --type ...")
        return

    listing = table(
        f"Alerts ({len(rows)} of {total})",
        ["ID", "Product", "Type", "Params", "Provider", "Cooldown", "Enabled", "Last fired"],
    )
    for row in rows:
        listing.add_row(*row)
    stdout.print(listing)


@alerts_app.command("set-cooldown")
def alerts_set_cooldown(
    rule_id: Annotated[int, typer.Argument(help="Alert rule ID.")],
    seconds: Annotated[
        int | None,
        typer.Argument(help="Minimum seconds between firings. Omit to remove the gap."),
    ] = None,
    user: UserOption = None,
) -> None:
    """Change how often an alert may fire, without recreating it."""
    try:
        with session_scope() as session:
            service = AlertService(session, acting_user(session, user).id)
            rule = service.set_cooldown(rule_id, seconds)
            gap = rule.cooldown_seconds
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc
    except ValidationError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc

    if gap is None:
        success(f"alert {rule_id} may now fire whenever its condition is met")
    else:
        success(f"alert {rule_id} will fire at most once every {gap}s")


@alerts_app.command("set-enabled")
def alerts_set_enabled(
    rule_id: Annotated[int, typer.Argument(help="Alert rule ID.")],
    on: Annotated[
        bool, typer.Option("--on/--off", help="Turn the alert on, or off.")
    ] = True,
    user: UserOption = None,
) -> None:
    """Turn an alert on or off without deleting it.

    A disabled rule stays attached to its product and keeps its cooldown and history; it
    simply stops firing until it is turned back on.
    """
    try:
        with session_scope() as session:
            service = AlertService(session, acting_user(session, user).id)
            rule = service.set_enabled(rule_id, on)
            state = rule.enabled
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc

    success(f"alert {rule_id} is now {'enabled' if state else 'disabled'}")


@alerts_app.command("remove")
def alerts_remove(
    rule_id: Annotated[int, typer.Argument(help="Alert ID.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
    user: UserOption = None,
) -> None:
    """Delete a tracking rule."""
    try:
        with session_scope() as session:
            rule = AlertService(session, acting_user(session, user).id).get(rule_id)
            label = f"{rule.rule_type.value} on product {rule.product_id}"
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc

    if not yes:
        typer.confirm(f"Delete alert {rule_id} ({label})?", abort=True)

    with session_scope() as session:
        AlertService(session, acting_user(session, user).id).remove(rule_id)
    success(f"removed alert {rule_id}")


@alerts_app.command("history")
def alerts_history(
    product_id: Annotated[int, typer.Argument(help="Product ID.")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
    user: UserOption = None,
) -> None:
    """Show notifications generated for a product."""
    with session_scope() as session:
        rows = NotificationRepository(session).list_for_product(product_id, limit=limit)
        rendered = [
            (
                str(n.id),
                n.created_at.strftime("%Y-%m-%d %H:%M"),
                n.event_type,
                _status_markup(n.status.value),
                n.provider or "-",
                str((n.payload or {}).get("title", ""))[:44],
            )
            for n in rows
        ]

    if not rendered:
        warn("no notifications for this product yet")
        return

    listing = table(
        f"Notifications for product {product_id}",
        ["ID", "Created", "Event", "Status", "Provider", "Title"],
    )
    for row in rendered:
        listing.add_row(*row)
    stdout.print(listing)


def pause(
    product_id: Annotated[int, typer.Argument(help="Product ID.")],
    user: UserOption = None,
) -> None:
    """Stop scheduled checks for a product. History and alerts are kept."""
    _set_status(product_id, TrackingStatus.PAUSED, user)
    success(f"paused product {product_id} (manual checks still work)")


def resume(
    product_id: Annotated[int, typer.Argument(help="Product ID.")],
    user: UserOption = None,
) -> None:
    """Resume scheduled checks for a product."""
    _set_status(product_id, TrackingStatus.ACTIVE, user)
    success(f"resumed product {product_id}")


def _set_status(product_id: int, status: TrackingStatus, user: str | None = None) -> None:
    try:
        with session_scope() as session:
            service = AlertService(session, acting_user(session, user).id)
            service.set_tracking_status(product_id, status)
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc


def _params_summary(params: dict[str, object]) -> str:
    if not params:
        return "[dim]-[/dim]"
    return ", ".join(f"{key}={value}" for key, value in sorted(params.items()))


def _status_markup(value: str) -> str:
    colour = {
        "sent": "green",
        "pending": "yellow",
        "failed": "red",
        "suppressed": "dim",
    }.get(value, "white")
    return f"[{colour}]{value}[/{colour}]"
