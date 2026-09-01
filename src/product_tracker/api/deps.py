"""FastAPI dependencies: database sessions, settings, pagination, and auth.

Auth is expressed as two dependencies rather than one so the distinction is visible at
each route: reading tracked products is a different exposure from adding or deleting them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..core.security import requires_key_for_reads, verify_api_key
from ..db.session import get_session_factory

#: Sent by clients that authenticate. Declared once so the header name cannot drift
#: between the dependency and the OpenAPI description.
API_KEY_HEADER = "X-API-Key"

ApiKeyHeader = Annotated[
    str | None,
    Header(alias=API_KEY_HEADER, description="Required when API_KEY is configured."),
]


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


def _unauthorised() -> HTTPException:
    """401 with the scheme advertised, so a client knows how to authenticate."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"missing or invalid {API_KEY_HEADER}",
        headers={"WWW-Authenticate": f'ApiKey realm="product-tracker", header="{API_KEY_HEADER}"'},
    )


def require_write(
    settings: Annotated[Settings, Depends(get_config)], api_key: ApiKeyHeader = None
) -> None:
    """Guard endpoints that change state. No-op when no API key is configured."""
    if not verify_api_key(settings, api_key):
        raise _unauthorised()


def require_read(
    settings: Annotated[Settings, Depends(get_config)], api_key: ApiKeyHeader = None
) -> None:
    """Guard read endpoints, when ``API_ALLOW_ANONYMOUS_READS`` is off."""
    if requires_key_for_reads(settings) and not verify_api_key(settings, api_key):
        raise _unauthorised()


DbSession = Annotated[Session, Depends(get_db)]
Config = Annotated[Settings, Depends(get_config)]
PageParams = Annotated[Pagination, Depends(pagination)]

#: Attach to any route that creates, changes, or deletes something.
RequireWrite = Depends(require_write)
#: Attached once to the versioned router; every route below it inherits the read check.
RequireRead = Depends(require_read)
