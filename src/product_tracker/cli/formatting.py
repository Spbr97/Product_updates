"""Console output helpers and exit codes.

Human-readable output goes to stdout; diagnostics go to stderr, so ``product-tracker
list | grep ...`` stays usable.
"""

from __future__ import annotations

from enum import IntEnum

from rich.console import Console
from rich.table import Table

stdout = Console()
stderr = Console(stderr=True)


class ExitCode(IntEnum):
    """Process exit codes. Scripts can branch on these."""

    OK = 0
    ERROR = 1  # Unexpected failure.
    NOT_FOUND = 2  # The requested product/rule does not exist.
    STORE_FAILURE = 3  # A store could not be read (blocked, timeout, no price).
    CONFIG_ERROR = 4  # Settings are missing or invalid.


def success(message: str) -> None:
    stdout.print(f"[green]OK[/green]  {message}")


def info(message: str) -> None:
    stdout.print(message)


def warn(message: str) -> None:
    stderr.print(f"[yellow]WARN[/yellow]  {message}")


def error(message: str) -> None:
    stderr.print(f"[red]ERROR[/red]  {message}")


def table(title: str, columns: list[str]) -> Table:
    result = Table(title=title, title_justify="left", header_style="bold")
    for column in columns:
        result.add_column(column, overflow="fold")
    return result


def yes_no(value: bool) -> str:
    return "[green]yes[/green]" if value else "[red]no[/red]"
