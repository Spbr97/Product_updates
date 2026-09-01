"""Liveness and readiness endpoints.

``/health`` must never touch a dependency -- an orchestrator uses it to decide whether to
restart the process, and a database outage is not a reason to kill the API.

``/health/ready`` reports each dependency and returns 503 when a *required* one is down.
The distinction matters: the database is required, because nothing works without it. The
worker and the notification providers are reported but not required -- an API that can
serve reads and accept new products is doing its job even if no worker is running, and
saying otherwise would take the whole service out of the load balancer over a background
problem.

Neither endpoint requires authentication: a probe should not need a credential.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from ... import __version__
from ...core.config import get_settings
from ...core.security import is_auth_enabled
from ...db.session import current_revision, get_session_factory, ping
from ...notifications.registry import provider_status
from ...scheduler import heartbeat
from ...scheduler.status import scheduler_status
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
    database = _database_status()
    dependencies = [database, *_optional_dependencies()]

    # Only the database gates readiness.
    if not database.healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if database.healthy else "not_ready",
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


def _optional_dependencies() -> list[DependencyStatus]:
    """Report the worker and notification channels without gating readiness on them."""
    return [_scheduler_status(), _notifications_status(), _auth_status()]


def _scheduler_status() -> DependencyStatus:
    try:
        session = get_session_factory()()
    except Exception as exc:
        return DependencyStatus(name="scheduler", healthy=False, detail=_short(exc))
    try:
        settings = get_settings()
        state = scheduler_status(session)
        liveness = heartbeat.read(
            session, reconcile_interval_seconds=settings.reconcile_interval_seconds
        )
    except Exception as exc:
        return DependencyStatus(name="scheduler", healthy=False, detail=_short(exc))
    finally:
        session.close()

    jobs = f"{state.product_jobs} product job(s)" if state.available else state.detail
    return DependencyStatus(
        name="scheduler",
        # A worker that has never started is not unhealthy: a fresh install has none.
        healthy=liveness.running is not False,
        detail=f"{jobs}; {liveness.detail}",
    )


def _notifications_status() -> DependencyStatus:
    try:
        settings = get_settings()
        rows = provider_status(settings)
    except Exception as exc:
        return DependencyStatus(name="notifications", healthy=False, detail=_short(exc))

    usable = [slug for slug, _name, enabled, configured in rows if enabled and configured]
    requested = [slug for slug, _name, enabled, _configured in rows if enabled]
    missing = sorted(set(requested) - set(usable))

    if not requested:
        return DependencyStatus(
            name="notifications", healthy=False, detail="no providers enabled; alerts go nowhere"
        )
    detail = f"active: {', '.join(sorted(usable)) or 'none'}"
    if missing:
        detail += f"; enabled but unconfigured: {', '.join(missing)}"
    return DependencyStatus(name="notifications", healthy=bool(usable), detail=detail)


def _auth_status() -> DependencyStatus:
    try:
        settings = get_settings()
    except Exception as exc:
        return DependencyStatus(name="auth", healthy=False, detail=_short(exc))

    if not is_auth_enabled(settings):
        return DependencyStatus(
            name="auth", healthy=True, detail="disabled (no API_KEY set); do not expose publicly"
        )
    scope = "writes" if settings.api_allow_anonymous_reads else "reads and writes"
    return DependencyStatus(name="auth", healthy=True, detail=f"API key required for {scope}")


def _short(exc: Exception) -> str:
    """A brief, non-leaking description of a failure.

    Connection errors can carry the DSN (and therefore a password), so only the exception
    class name is exposed.
    """
    return type(exc).__name__
