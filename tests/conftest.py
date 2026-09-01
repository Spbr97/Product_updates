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


# --- Database-backed fixtures ------------------------------------------------------


def _test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    if not url:
        pytest.skip(
            "TEST_DATABASE_URL is not set. Start PostgreSQL "
            "(docker compose -f docker/docker-compose.yml up -d db) and set it to a "
            "throwaway database to run integration tests."
        )
    return url


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


#: Tables holding test-created data. ``stores`` is excluded -- it is seeded by migration
#: and products reference it.
_DATA_TABLES = (
    "notifications",
    "tracking_rules",
    "availability_history",
    "price_history",
    "check_executions",
    "products",
    # Grouping tables: without these a group created by one test is still there for the
    # next one, which quietly turns "list the groups" assertions into order-dependent
    # nonsense. Listed after products because products reference variants.
    "product_variants",
    "product_groups",
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
    with get_engine().begin() as connection:
        connection.execute(statement)
    yield
    with get_engine().begin() as connection:
        connection.execute(statement)


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
