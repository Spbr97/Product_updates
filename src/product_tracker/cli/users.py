"""User commands, and deciding which account the CLI acts as.

The CLI is not authenticated, deliberately. It talks to the database directly, so anyone
who can run it can already read the database with ``psql`` -- asking it for an API key would
be security theatre. What it needs instead is to know *whose* subscriptions, groups and
alerts a command refers to, which is what ``--user`` and ``PRODUCT_TRACKER_USER`` select.

The API is the boundary that authenticates. This is the operator's console.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer
from sqlalchemy.orm import Session

from ..db.models import User
from ..db.session import session_scope
from ..domain.errors import DuplicateError, NotFoundError, ValidationError
from ..repositories.users import UserRepository
from ..services import user_service
from .formatting import ExitCode, error, info, stdout, success, table, warn

users_app = typer.Typer(help="Manage accounts.", no_args_is_help=True)

#: Selects the account CLI commands act as, when ``--user`` is not given.
USER_ENV_VAR = "PRODUCT_TRACKER_USER"


def acting_user(session: Session, override: str | None = None) -> User:
    """Which account this command acts as.

    ``--user``, then ``PRODUCT_TRACKER_USER``, then the default account. Accepts an id or
    an email so scripts can use whichever is stable for them.
    """
    identifier = override or os.environ.get(USER_ENV_VAR)
    if not identifier:
        return user_service.default_user(session)
    key: int | str = int(identifier) if identifier.isdigit() else identifier
    return user_service.get_user(session, key)


UserOption = Annotated[
    str | None,
    typer.Option("--user", help=f"Account id or email. Defaults to ${USER_ENV_VAR}."),
]


def list_users() -> None:
    """List accounts."""
    with session_scope() as session:
        repo = UserRepository(session)
        users = repo.list_all()
        counts = repo.subscription_counts()

        listing = table("Accounts", ["Id", "Email", "Name", "Key", "Active", "Admin", "Watching"])
        for user in users:
            listing.add_row(
                str(user.id),
                user.email,
                user.name or "-",
                # Never the key, and never the hash: neither is useful to show, and one of
                # them is a credential.
                "[green]set[/green]" if user.api_key_hash else "[dim]none[/dim]",
                "yes" if user.is_active else "[red]no[/red]",
                "yes" if user.is_admin else "-",
                str(counts.get(user.id, 0)),
            )
        stdout.print(listing)


def add_user(
    email: Annotated[str, typer.Argument(help="Email address, used as the account id.")],
    name: Annotated[str | None, typer.Option("--name", help="Display name.")] = None,
    admin: Annotated[bool, typer.Option("--admin", help="Grant admin.")] = False,
    with_key: Annotated[
        bool, typer.Option("--key/--no-key", help="Issue an API key.")
    ] = True,
) -> None:
    """Create an account and print its API key once."""
    try:
        with session_scope() as session:
            first_key = with_key and not UserRepository(session).any_key_configured()
            created = user_service.create_user(
                session, email=email, name=name, is_admin=admin, with_key=with_key
            )
            user_id, api_key = created.user.id, created.api_key
    except ValidationError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc
    except DuplicateError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.ERROR) from exc

    success(f"created account {user_id} ({email})")
    if api_key:
        _print_key(api_key)
    if first_key:
        warn(
            "This is the first account with a key, so the API now requires one on every "
            "request. Existing unauthenticated clients will start getting 401s."
        )


def rotate_key(
    identifier: Annotated[str, typer.Argument(help="Account id or email.")],
) -> None:
    """Issue a new API key, invalidating the old one immediately."""
    try:
        with session_scope() as session:
            created = user_service.rotate_key(
                session, int(identifier) if identifier.isdigit() else identifier
            )
            api_key = created.api_key
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc

    success(f"issued a new key for {identifier}; the previous key no longer works")
    _print_key(api_key)


def set_active(
    identifier: Annotated[str, typer.Argument(help="Account id or email.")],
    active: Annotated[bool, typer.Option("--active/--inactive")] = True,
) -> None:
    """Enable or disable an account without deleting what it owns."""
    try:
        with session_scope() as session:
            user_service.set_active(
                session,
                int(identifier) if identifier.isdigit() else identifier,
                active=active,
            )
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc
    success(f"{identifier} is now {'active' if active else 'inactive'}")


def remove_user(
    identifier: Annotated[str, typer.Argument(help="Account id or email.")],
) -> None:
    """Delete an account, its subscriptions, groups and alerts.

    Tracked listings and price history are shared, so they are never removed here.
    """
    try:
        with session_scope() as session:
            user_service.delete_user(
                session, int(identifier) if identifier.isdigit() else identifier
            )
    except NotFoundError as exc:
        error(str(exc))
        raise typer.Exit(ExitCode.NOT_FOUND) from exc
    success(f"removed {identifier} (tracked listings and history kept)")


def _print_key(api_key: str) -> None:
    """Show a key once. It is stored only as a hash and cannot be recovered."""
    stdout.print()
    stdout.print(f"  [bold]{api_key}[/bold]")
    stdout.print()
    info("Copy this now: only a hash is stored, so it cannot be shown again.")


users_app.command("list")(list_users)
users_app.command("add")(add_user)
users_app.command("rotate-key")(rotate_key)
users_app.command("set-active")(set_active)
users_app.command("remove")(remove_user)
