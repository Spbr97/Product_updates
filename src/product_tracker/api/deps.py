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
from ..db.models import User
from ..db.session import get_session_factory
from ..repositories.users import UserRepository
from ..services.user_service import default_user, resolve_user

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


def current_user(
    session: DbSession,
    settings: Annotated[Settings, Depends(get_config)],
    api_key: ApiKeyHeader = None,
) -> User:
    """The account this request acts as.

    With authentication switched off -- no ``API_KEY`` and no user holding a key -- this is
    the default account, so a single-user install needs no credential and behaves exactly
    as it did before accounts existed.
    """
    user = resolve_user(session, settings, api_key)
    if user is None:
        raise _unauthorised()
    return user


def current_reader(
    session: DbSession,
    settings: Annotated[Settings, Depends(get_config)],
    api_key: ApiKeyHeader = None,
) -> User:
    """The account a *read* acts as.

    ``API_ALLOW_ANONYMOUS_READS`` stops applying once any user holds their own key, and
    that is not an oversight. Reads used to expose one global dataset; now they expose a
    particular person's subscriptions, groups, and alerts, and an unidentified caller
    cannot be scoped to any of them. Serving them the default account's data instead would
    hand one user's watchlist to anyone who asked.
    """
    user = resolve_user(session, settings, api_key)
    if user is not None:
        return user
    if not settings.api_allow_anonymous_reads:
        raise _unauthorised()
    if UserRepository(session).any_key_configured():
        raise _unauthorised()
    return default_user(session)


CurrentUser = Annotated[User, Depends(current_user)]
CurrentReader = Annotated[User, Depends(current_reader)]
