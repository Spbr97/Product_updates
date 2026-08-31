"""Engine and session management.

The engine is created once per process and cached. ``session_scope`` is the only way the
application opens a transaction, so commit/rollback handling lives in exactly one place.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import Settings, get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Create (once) the process-wide engine.

    ``pool_pre_ping`` matters here: worker processes hold connections idle between
    scheduled checks, and a Postgres restart or a connection reaped by a proxy would
    otherwise surface as a hard failure on the next check.
    """
    settings = get_settings()
    return _build_engine(settings)


def _build_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # libpq gives up on an unreachable host rather than blocking indefinitely, which
        # is what lets /health/ready answer "not ready" instead of hanging.
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
        future=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Cached session factory bound to the process engine."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Run a unit of work in a transaction, committing on success.

    Any exception rolls back and propagates -- callers that want to tolerate a failure
    (the tracking engine, for instance) catch it themselves.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping(session: Session) -> bool:
    """Return True if the database answers a trivial query."""
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


def current_revision(session: Session) -> str | None:
    """The Alembic revision the database is stamped with, or None if unmigrated."""
    try:
        result = session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        row = result.first()
    except Exception:
        return None
    return str(row[0]) if row else None


def reset_engine_cache() -> None:
    """Dispose the engine and drop caches. For tests that switch databases."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
