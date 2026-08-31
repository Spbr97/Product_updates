"""FastAPI dependencies: database sessions, settings, and pagination.

Auth is introduced in phase 6; the ``require_write`` dependency is declared here now so
routers can depend on it from the start and gain enforcement without being edited.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..db.session import get_session_factory


def get_db() -> Iterator[Session]:
    """Yield a request-scoped session, committing on success.

    Mirrors ``session_scope`` but as a generator dependency so FastAPI controls the
    lifetime and exceptions still roll back before the handler in ``errors.py`` runs.
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


def get_config() -> Settings:
    return get_settings()


class Pagination(BaseModel):
    limit: int
    offset: int


def pagination(
    settings: Annotated[Settings, Depends(get_config)],
    limit: Annotated[int | None, Query(ge=1, description="Rows per page.")] = None,
    offset: Annotated[int, Query(ge=0, description="Rows to skip.")] = 0,
) -> Pagination:
    """Clamp pagination to configured bounds so a client cannot request the whole table."""
    effective = settings.api_default_page_size if limit is None else limit
    return Pagination(limit=min(effective, settings.api_max_page_size), offset=offset)


DbSession = Annotated[Session, Depends(get_db)]
Config = Annotated[Settings, Depends(get_config)]
PageParams = Annotated[Pagination, Depends(pagination)]
