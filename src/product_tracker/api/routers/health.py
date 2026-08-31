"""Liveness and readiness endpoints.

``/health`` must never touch a dependency -- an orchestrator uses it to decide whether to
restart the process, and a database outage is not a reason to kill the API.

``/health/ready`` reports each dependency and returns 503 when a required one is down, so
a load balancer stops sending traffic while the process stays alive.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from ... import __version__
from ...db.session import current_revision, get_session_factory, ping
from ..schemas.common import DependencyStatus, HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"description": "A required dependency is unavailable."}},
)
def readiness(response: Response) -> ReadinessResponse:
    dependencies = [_database_status()]
    ready = all(dep.healthy for dep in dependencies)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        version=__version__,
        dependencies=dependencies,
    )


def _database_status() -> DependencyStatus:
    """Check connectivity and that migrations have been applied.

    A reachable but unmigrated database is not ready: every query would fail on a missing
    table, which is worse to debug than an explicit "not ready".
    """
    try:
        session = get_session_factory()()
    except Exception as exc:
        return DependencyStatus(name="database", healthy=False, detail=_short(exc))

    try:
        if not ping(session):
            return DependencyStatus(name="database", healthy=False, detail="query failed")
        revision = current_revision(session)
        if revision is None:
            return DependencyStatus(
                name="database",
                healthy=False,
                detail="no alembic_version: run 'alembic upgrade head'",
            )
        return DependencyStatus(name="database", healthy=True, detail=f"revision {revision}")
    except Exception as exc:
        return DependencyStatus(name="database", healthy=False, detail=_short(exc))
    finally:
        session.close()


def _short(exc: Exception) -> str:
    """A brief, non-leaking description of a failure.

    Connection errors can carry the DSN (and therefore a password), so only the exception
    class name is exposed.
    """
    return type(exc).__name__
