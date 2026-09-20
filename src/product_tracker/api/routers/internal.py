"""The internal scheduler trigger.

For a host that cannot keep the background worker (``product-tracker worker``) alive --
a free-tier web service that sleeps between requests, for instance -- an external cron
calls this instead, on the interval ``CHECK_INTERVAL_SECONDS`` implies. It runs the same
bulk sweep the CLI's ``check-all`` runs, through the same claim guard the live worker uses,
so this is safe to call on a schedule even if a worker also happens to be running.

Unversioned and outside ``/api/v1`` on purpose: this is not a resource a client of the
tracker reads or writes, it is an operational trigger, guarded by its own secret.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ...services.check_runner import run_all_checks
from ..deps import Config, RequireInternalToken
from ..schemas.internal import CheckAllResponse

router = APIRouter(
    prefix="/internal/scheduler", tags=["internal"], dependencies=[RequireInternalToken]
)


@router.post(
    "/check-all",
    response_model=CheckAllResponse,
    summary="Check every schedulable product now",
    responses={401: {"description": "Missing or invalid X-Internal-Token."}},
)
def check_all(
    settings: Config,
    limit: Annotated[
        int, Query(ge=1, le=1000, description="Maximum products to check in this sweep.")
    ] = 100,
) -> CheckAllResponse:
    summary = run_all_checks(settings, limit=limit)
    return CheckAllResponse(
        status="completed",
        checked=summary.checked,
        skipped=summary.skipped,
        failures=summary.failed,
        notifications_sent=summary.delivery.sent,
        notifications_failed=summary.delivery.failed,
    )
