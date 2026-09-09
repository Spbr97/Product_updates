"""``product-tracker db``: bring the database up, check it, take it down.

PostgreSQL is not negotiable for this system -- concurrent writers, ``SELECT … FOR
UPDATE`` pacing shared across processes, advisory locks, JSONB rule params, and a partial
unique index all depend on it, and every embedded or file-backed alternative gives at
least one of those up. What *was* negotiable is how much of that a person has to know
about to start.

Before this, the answer to "how do I run it?" was: install Docker, learn compose, find the
right file, know which service to start, then set two environment variables to a DSN whose
host must be the IPv4 literal. That is a real cost, and it is the only honest complaint
about choosing a server database for a local tool. So it is one command now.

This wraps Docker rather than downloading and managing a PostgreSQL cluster directly:
Docker is already a dependency of this project, and a bundled cluster would mean shipping
a ~300 MB per-platform binary and writing a service manager -- more moving parts to be
wrong, to solve a problem `docker compose up` already solves.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from .formatting import ExitCode, error, info, stdout, success, table, warn

db_app = typer.Typer(help="Start, check and stop the PostgreSQL the tracker uses.")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = PROJECT_ROOT / "docker" / "docker-compose.yml"

#: The DSN the compose file serves. The IPv4 literal is deliberate: the port is published
#: on the IPv4 loopback only, and `localhost` resolves to ::1 first, costing ~10s per
#: connection while that attempt times out.
LOCAL_DSN = "postgresql+psycopg://tracker:tracker@127.0.0.1:5432/{database}"


def _compose(*args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _daemon_running() -> bool:
    if not _docker_available():
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=60
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _start_docker_desktop() -> bool:
    """Launch Docker Desktop on Windows and wait for the daemon. False if unavailable."""
    if sys.platform != "win32":
        return False
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DockerDesktop/Docker Desktop.exe",
        Path("C:/Program Files/Docker/Docker/Docker Desktop.exe"),
    ]
    exe = next((p for p in candidates if p.exists()), None)
    if exe is None:
        return False
    info("starting Docker Desktop…")
    subprocess.Popen([str(exe)], close_fds=True)
    for _ in range(120):  # up to two minutes; a cold start is genuinely slow
        if _daemon_running():
            return True
        time.sleep(1)
    return False


def _require_docker() -> None:
    if _daemon_running():
        return
    if not _docker_available():
        error("Docker is not installed, and it is how this project runs PostgreSQL.")
        info("  Install Docker Desktop: https://docs.docker.com/get-started/get-docker/")
        info("  Or point DATABASE_URL at any PostgreSQL 14+ you already run.")
        raise typer.Exit(ExitCode.CONFIG_ERROR)
    if not _start_docker_desktop():
        error("Docker is installed but its daemon is not running.")
        info("  Start Docker Desktop, then run this again.")
        raise typer.Exit(ExitCode.CONFIG_ERROR)


def _healthy(service: str = "db") -> bool:
    result = _compose("ps", "--format", "{{.Service}} {{.Health}}")
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == service:
            return parts[1] == "healthy"
    return False


@db_app.command("up")
def up(
    wait_seconds: Annotated[
        int, typer.Option("--timeout", min=10, max=600, help="How long to wait for health.")
    ] = 180,
    with_test_db: Annotated[
        bool,
        typer.Option("--test-db/--no-test-db", help="Also create the throwaway test database."),
    ] = True,
) -> None:
    """Start PostgreSQL and migrate it to head.

    Starts Docker itself if it is installed but not running, so "nothing is set up yet"
    and "it is already running" both end in a working database.
    """
    _require_docker()

    info("starting PostgreSQL…")
    started = _compose("up", "-d", "db")
    if started.returncode != 0:
        error("could not start the database container")
        stdout.print(started.stderr.strip()[:800])
        raise typer.Exit(ExitCode.ERROR)

    deadline = time.time() + wait_seconds
    while time.time() < deadline and not _healthy():
        time.sleep(2)
    if not _healthy():
        error(f"the database did not become healthy within {wait_seconds}s")
        raise typer.Exit(ExitCode.ERROR)

    info("applying migrations…")
    migrated = _compose("up", "migrate", "--exit-code-from", "migrate")
    if migrated.returncode != 0:
        error("migrations failed")
        stdout.print(migrated.stdout.strip()[-800:])
        raise typer.Exit(ExitCode.ERROR)

    if with_test_db:
        _compose(
            "exec", "-T", "db", "psql", "-U", "tracker", "-c",
            "CREATE DATABASE tracker_test OWNER tracker",
        )  # already-exists is fine; the error is not worth reporting

    success("PostgreSQL is up and migrated")
    _print_dsns(with_test_db)


def _print_dsns(with_test_db: bool) -> None:
    settings = table("Point your environment at it", ["Variable", "Value"])
    settings.add_row("DATABASE_URL", LOCAL_DSN.format(database="tracker"))
    if with_test_db:
        settings.add_row("TEST_DATABASE_URL", LOCAL_DSN.format(database="tracker_test"))
    stdout.print(settings)
    if not (PROJECT_ROOT / ".env").exists():
        info("  No .env yet:  Copy-Item .env.example .env")
    info("  Then:  product-tracker init   (see tracking.example.yaml)")


@db_app.command("status")
def status() -> None:
    """Whether the database is running, healthy, and reachable."""
    if not _daemon_running():
        warn("Docker is not running, so the database cannot be up.")
        info("  Start it with:  product-tracker db up")
        raise typer.Exit(ExitCode.NOT_FOUND)

    healthy = _healthy()
    state = table("Database", ["Check", "Result"])
    state.add_row("container", "[green]healthy[/green]" if healthy else "[red]not running[/red]")

    reachable, detail = _probe()
    state.add_row("reachable", "[green]yes[/green]" if reachable else f"[red]no[/red] ({detail})")
    stdout.print(state)

    if not (healthy and reachable):
        info("  Start it with:  product-tracker db up")
        raise typer.Exit(ExitCode.NOT_FOUND)
    success("the database is up and reachable")


def _probe() -> tuple[bool, str]:
    """Open one real connection, because a healthy container is not the same as reachable."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    from ..core.config import get_settings

    try:
        dsn = get_settings().database_url
    except Exception:
        dsn = LOCAL_DSN.format(database="tracker")
    try:
        engine = create_engine(dsn, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except SQLAlchemyError as exc:
        return False, type(exc).__name__
    return True, ""


@db_app.command("down")
def down(
    delete_data: Annotated[
        bool,
        typer.Option(
            "--delete-data",
            help="Also delete the volume. Destroys every price recorded.",
        ),
    ] = False,
) -> None:
    """Stop the database. Your data survives unless you ask otherwise."""
    _require_docker()
    if delete_data:
        warn("this deletes the volume: every product, price and alert recorded is destroyed")
        typer.confirm("Delete all tracked data?", abort=True)
        _compose("down", "-v", capture=False)
        success("stopped, and the data volume deleted")
        return
    _compose("stop", "db", "migrate", capture=False)
    success("stopped. Your data is still on the volume; `db up` brings it back")
