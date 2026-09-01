"""Typer application -- the ``product-tracker`` command.

Product, alert, and worker commands are registered here as later phases land. The
callback configures logging once for every invocation so a command can be run with
``--log-level DEBUG`` without editing the environment.
"""

from __future__ import annotations

from typing import Annotated

import typer

from .. import __version__
from ..core.config import get_settings
from ..core.logging import configure_logging
from ..domain.errors import ConfigurationError
from . import history, products, system
from .formatting import ExitCode, error, stdout

app = typer.Typer(
    name="product-tracker",
    help="Track product prices and availability across e-commerce sites.",
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(system.stores_app, name="stores")
app.command("status")(system.status)
app.command("config")(system.config_show)

app.command("add")(products.add)
app.command("list")(products.list_products)
app.command("show")(products.show)
app.command("remove")(products.remove)
app.command("check")(products.check)
app.command("history")(history.history)


def _version_callback(value: bool) -> None:
    if value:
        stdout.print(f"product-tracker {__version__}")
        raise typer.Exit(ExitCode.OK)


@app.callback()
def main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", help="Override LOG_LEVEL for this invocation."),
    ] = None,
) -> None:
    """Configure logging before any command runs.

    Settings failures are tolerated here so that ``--version`` and ``--help`` keep working
    on an unconfigured machine; commands that need settings report the problem themselves.
    """
    try:
        settings = get_settings()
        level = log_level or settings.log_level
        fmt = settings.log_format
    except ConfigurationError:
        level = log_level or "WARNING"
        fmt = "console"
    configure_logging(level=level.upper(), fmt=fmt)


def run() -> None:
    """Console-script wrapper that turns uncaught domain errors into exit codes."""
    try:
        app()
    except ConfigurationError as exc:
        error(str(exc))
        raise SystemExit(ExitCode.CONFIG_ERROR) from exc


if __name__ == "__main__":
    run()
