"""Shared test fixtures.

Two tiers of test:

* **Unit** -- no database, no network. They run anywhere.
* **Integration** (``@pytest.mark.db``) -- need a real PostgreSQL, because that is what the
  application uses; there is no SQLite fallback to keep honest. They are skipped with a
  clear message when ``TEST_DATABASE_URL`` is not set.

``TEST_DATABASE_URL`` must point at a throwaway database: the schema is migrated up at the
start of the session and torn back down at the end.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: A syntactically valid DSN that is never connected to. Lets unit tests construct
#: Settings and the FastAPI app without a live database.
DUMMY_DSN = "postgresql+psycopg://user:pass@localhost:5432/unit_tests"


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stop a developer's real .env, and other tests, from leaking into this one.

    Both caches are dropped, not just settings: the engine is derived from settings but
    cached independently, so clearing only settings would leave a live engine still bound
    to the previous test's database. That is not hypothetical -- it made the
    "database is down" readiness test pass against a real database.
    """
    from product_tracker.core.config import Settings, reset_settings_cache
    from product_tracker.db.session import reset_engine_cache

    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
    # Settings reads .env by default; point it somewhere that does not exist.
    monkeypatch.setenv("PRODUCT_TRACKER_TEST_MODE", "1")
    # Rich wraps tables to the terminal width; the default 80 columns truncates long
    # values like "price_below_target" and makes output assertions fail spuriously.
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(PROJECT_ROOT / "tests")

    reset_settings_cache()
    reset_engine_cache()
    yield
    reset_settings_cache()
    reset_engine_cache()


@pytest.fixture
def dummy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal valid configuration for tests that never touch the database."""
    from product_tracker.core.config import reset_settings_cache

    monkeypatch.setenv("DATABASE_URL", DUMMY_DSN)
    monkeypatch.setenv("LOG_FORMAT", "console")
    # Tests that assert on "database is down" should not wait out the production timeout.
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("PLAYWRIGHT_ENABLED", "false")
    reset_settings_cache()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render CLI tables at a fixed width, whatever the terminal is.

    Rich sizes a table to the console, and elides cell content that will not fit. The CLI
    tests assert on what a table *says*, not how it is laid out, so without this they pass
    or fail on the width of whoever's terminal is running them. That is not theoretical:
    they passed locally and in serial CI for months, then three of them failed the moment
    pytest-xdist changed the width Rich detected. Reproducible with COLUMNS=80.

    Setting COLUMNS is not enough: Rich ignores it when the output is not a terminal, and
    CliRunner captures it. The width is set on the Console objects themselves, which every
    CLI module shares by importing the same instance.
    """
    from product_tracker.cli import formatting

    for console in (formatting.stdout, formatting.stderr):
        monkeypatch.setattr(console, "_width", 200, raising=False)


@pytest.fixture(autouse=True)
def _isolated_sitemap_cache(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give every test its own sitemap cache directory.

    ``sitemaps.cache_dir()`` is a single folder under the system temp, shared by every
    process on the machine. That is right in production -- a downloaded catalogue is
    worth reusing between runs -- and wrong under ``pytest -n``, where two dozen workers
    read, rewrite and delete each other's ``shop.json`` and the suite goes flaky: three
    consecutive full runs gave a pass, an error and a failure before this.

    Isolating it also stops a developer's real cached catalogues from changing what the
    tests see, which is the same bug wearing a different hat.
    """
    from product_tracker.stores import sitemaps

    directory = tmp_path_factory.mktemp("sitemaps")
    monkeypatch.setattr(sitemaps, "cache_dir", lambda: directory)


# --- Database-backed fixtures ------------------------------------------------------


def _test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


def _ensure_database(url: str) -> None:
    """Create the target database if it does not exist yet.

    Only reached under xdist, where each worker gets its own. Connects to ``postgres``
    to do it, because you cannot create a database from inside itself.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    target = make_url(url)
    engine = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            found = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).scalar()
            if not found:
                conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def database_url(worker_id: str) -> str:
    """The throwaway database this session migrates.

    Under ``pytest -n``, each worker gets its own: they migrate up at session start and
    down at the end, and truncate between tests, so a shared database would have workers
    dropping tables out from under each other. The name is suffixed with the worker id
    (``tracker_test_gw3``) and created on demand, then left in place between runs -- a
    re-run reuses it rather than paying to recreate it. They hold no data: the session
    fixture migrates down to base on the way out.
    """
    url = _test_database_url()
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL is not set. Start PostgreSQL "
            "(docker compose -f docker/docker-compose.yml up -d db) and set it to a "
            "throwaway database to run integration tests."
        )
    if worker_id == "master":
        return url

    from sqlalchemy.engine import make_url

    target = make_url(url)
    scoped = target.set(database=f"{target.database}_{worker_id}").render_as_string(
        hide_password=False
    )
    _ensure_database(scoped)
    return scoped



@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> Iterator[str]:
    """Migrate a throwaway database to head for the session, then tear it down."""
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url

    from product_tracker.core.config import reset_settings_cache
    from product_tracker.db.session import reset_engine_cache

    reset_settings_cache()
    reset_engine_cache()

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))

    command.upgrade(config, "head")
    try:
        yield database_url
    finally:
        command.downgrade(config, "base")
        reset_engine_cache()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        reset_settings_cache()


@pytest.fixture
def db_env(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point settings at the migrated test database for a single test.

    The SSRF guard is off by default here. It resolves DNS, and test URLs use hostnames
    that deliberately do not exist -- leaving it on would make every test depend on a
    working resolver. Tests that exercise the guard turn it back on via
    :func:`strict_url_policy` and use IP literals, which need no lookup.
    """
    from product_tracker.core.config import reset_settings_cache
    from product_tracker.db.session import reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", migrated_database)
    monkeypatch.setenv("BLOCK_PRIVATE_ADDRESSES", "false")
    # Tests must never launch a browser. Playwright may be installed in the environment,
    # in which case a failed HTTP fetch would fall back to it and spend ~12s trying to
    # start Chromium. Browser behaviour is covered by stubbing `stores.browser.render`.
    monkeypatch.setenv("PLAYWRIGHT_ENABLED", "false")
    reset_settings_cache()
    reset_engine_cache()


@pytest.fixture
def strict_url_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-enable the SSRF guard for a test. Use IP literals to avoid a DNS lookup."""
    from product_tracker.core.config import reset_settings_cache

    monkeypatch.setenv("BLOCK_PRIVATE_ADDRESSES", "true")
    reset_settings_cache()


#: The account seeded by migration 0007, which ``clean_db`` preserves.
DEFAULT_USER_EMAIL = "local@localhost"

#: Tables holding test-created data. ``stores`` is excluded -- it is seeded by migration
#: and products reference it. ``users`` too: the default account is seeded, and the rest
#: are removed by the DELETE in ``clean_db``.
_DATA_TABLES = (
    "notifications",
    "tracking_rules",
    "availability_history",
    "price_history",
    "check_executions",
    # Entry tables before products: retailer_listings references products with RESTRICT,
    # so truncating products first would be refused. TRUNCATE ... CASCADE covers it, but
    # naming them keeps the order honest and the intent readable.
    "retailer_listing_url_audits",
    "retailer_listings",
    "product_entries",
    "products",
    # Grouping tables: without these a group created by one test is still there for the
    # next one, which quietly turns "list the groups" assertions into order-dependent
    # nonsense. Listed after products because products reference variants.
    "product_variants",
    "product_groups",
    "subscriptions",
    # Scheduler jobs and heartbeats too: a test that touches either would otherwise
    # leave rows that make a later test believe a worker is scheduled or alive.
    "apscheduler_jobs",
    "worker_heartbeats",
)


@pytest.fixture
def clean_db(db_env: None) -> Iterator[None]:
    """Start from an empty database, with identity sequences reset.

    Needed by CLI and API tests: those open their own sessions and commit, so the
    rollback in ``db_session`` cannot undo them. Resetting identities also makes ``1`` a
    predictable first product id.
    """
    from sqlalchemy import text

    from product_tracker.db.session import get_engine

    statement = text(
        f"TRUNCATE {', '.join(_DATA_TABLES)} RESTART IDENTITY CASCADE"
    )
    # Accounts created by a test are committed, so they outlive it. The seeded default
    # account is left alone, like the seeded stores -- but any other must go, or the first
    # test to create a keyed user silently switches API authentication on for every test
    # that runs after it.
    purge_users = text("DELETE FROM users WHERE email <> :default_email")

    def reset() -> None:
        with get_engine().begin() as connection:
            connection.execute(statement)
            connection.execute(purge_users, {"default_email": DEFAULT_USER_EMAIL})

    reset()
    yield
    reset()


@pytest.fixture
def db_session(db_env: None) -> Iterator[object]:
    """A session rolled back at the end of the test, so tests do not see each other."""
    from sqlalchemy.orm import Session

    from product_tracker.db.session import get_engine

    connection = get_engine().connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def owner_id(db_env: None) -> int:
    """The seeded default account.

    Groups, alert rules and subscriptions all belong to a user now, so tests need an owner
    to attribute them to. Migration 0007 seeds this account, and ``clean_db`` leaves it
    alone in the same way it leaves the seeded stores alone.
    """
    from product_tracker.db.session import session_scope
    from product_tracker.services.user_service import default_user

    with session_scope() as session:
        return int(default_user(session).id)
