"""Reading scheduler state without running a scheduler.

The API and ``product-tracker status`` need to answer "is anything actually checking these
products?", and they run in different processes from the worker. The honest source is the
job store itself: it is in PostgreSQL, so any process can read it.

Worker liveness is *inferred*, not measured. If jobs exist but the earliest one is long
overdue, nothing is running them. That is a reasonable signal and it is reported as an
inference rather than a fact -- a dedicated heartbeat would be the way to know for certain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from .apscheduler_queue import JOB_TABLE
from .jobqueue import product_id_from

#: How far past its due time a job must be before we call the worker stopped. Generous:
#: a check can take a while, and a brief pause should not be reported as an outage.
OVERDUE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    """What the job store says about scheduled work."""

    #: False when the job table does not exist yet (migrations not applied).
    available: bool
    total_jobs: int = 0
    product_jobs: int = 0
    next_run_at: datetime | None = None
    overdue_seconds: int = 0
    detail: str = ""

    @property
    def worker_running(self) -> bool | None:
        """True, False, or None when there is nothing to infer from."""
        if not self.available or self.total_jobs == 0:
            return None
        return self.overdue_seconds <= OVERDUE_SECONDS


def scheduler_status(session: Session) -> SchedulerStatus:
    """Read the APScheduler job store. Never raises."""
    try:
        rows = session.execute(
            # JOB_TABLE is a module constant, not user input.
            text(f"SELECT id, next_run_time FROM {JOB_TABLE}")
        ).all()
    except Exception:
        return SchedulerStatus(
            available=False, detail="job table not found; run 'alembic upgrade head'"
        )

    if not rows:
        return SchedulerStatus(available=True, detail="no jobs scheduled")

    product_jobs = sum(1 for job_id, _ in rows if product_id_from(str(job_id)) is not None)
    # A paused job has no next_run_time; it should not count towards "overdue".
    due = [float(next_run) for _, next_run in rows if next_run is not None]

    if not due:
        return SchedulerStatus(
            available=True,
            total_jobs=len(rows),
            product_jobs=product_jobs,
            detail="all jobs are paused",
        )

    earliest = min(due)
    next_run_at = datetime.fromtimestamp(earliest, tz=UTC)
    overdue = max(0, int(datetime.now(UTC).timestamp() - earliest))

    return SchedulerStatus(
        available=True,
        total_jobs=len(rows),
        product_jobs=product_jobs,
        next_run_at=next_run_at,
        overdue_seconds=overdue,
        detail=(
            f"no worker appears to be running; earliest job is {overdue}s overdue"
            if overdue > OVERDUE_SECONDS
            else "worker appears to be running"
        ),
    )
