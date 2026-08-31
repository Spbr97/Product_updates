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
    """Stop a developer's real .env from leaking into tests.

    Every ``Settings``-affecting variable is cleared, and the cache is dropped before and
    after each test so one test's environment cannot bleed into the next.
    """
    from product_tracker.core.config import Settings, reset_settings_cache

    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
    # Settings reads .env by default; point it somewhere that does not exist.
    monkeypatch.setenv("PRODUCT_TRACKER_TEST_MODE", "1")
    monkeypatch.chdir(PROJECT_ROOT / "tests")

    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def dummy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal valid configuration for tests that never touch the database."""
    from product_tracker.core.config import reset_settings_cache

    monkeypatch.setenv("DATABASE_URL", DUMMY_DSN)
    monkeypatch.setenv("LOG_FORMAT", "console")
    # Tests that assert on "database is down" should not wait out the production timeout.
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "2")
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
    """Point settings at the migrated test database for a single test."""
    from product_tracker.core.config import reset_settings_cache
    from product_tracker.db.session import reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", migrated_database)
    reset_settings_cache()
    reset_engine_cache()


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
