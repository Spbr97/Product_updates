"""APScheduler implementation of :class:`JobQueue`.

Jobs are persisted in PostgreSQL, so a worker restart resumes the existing schedule
instead of starting from scratch and re-checking everything at once.

Three settings do the real work of keeping the schedule honest:

* ``replace_existing`` with a deterministic job id -- scheduling a product twice replaces
  its job. Duplicate jobs are impossible by construction, not by convention.
* ``max_instances=1`` -- a check that runs longer than its interval does not get a second
  copy started on top of it.
* ``coalesce=True`` -- a worker that was down for six hours runs each product's missed
  check once on resume, not once for every interval it slept through.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..core.logging import EVENT_JOB_SCHEDULED, get_logger
from .jobqueue import JobQueue, job_id_for, product_id_from

log = get_logger(__name__)

#: APScheduler's own table. Created by migration 0003 so ``alembic downgrade base`` leaves
#: a clean database; APScheduler's create-if-missing is then a no-op.
JOB_TABLE = "apscheduler_jobs"

#: How late a job may run and still be run at all. Longer than any check should take, so a
#: brief worker outage does not silently drop that interval's checks.
MISFIRE_GRACE_SECONDS = 3600


class APSchedulerJobQueue(JobQueue):
    def __init__(
        self,
        database_url: str,
        *,
        check_callable: Callable[[int], None],
        max_workers: int = 4,
        timezone: str = "UTC",
    ) -> None:
        self._check = check_callable
        self._scheduler = BackgroundScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=database_url, tablename=JOB_TABLE)},
            executors={"default": ThreadPoolExecutor(max_workers=max_workers)},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": MISFIRE_GRACE_SECONDS,
            },
            timezone=timezone,
        )

    @property
    def scheduler(self) -> BackgroundScheduler:
        return self._scheduler

    def start(self, *, paused: bool = False) -> None:
        """Start the scheduler. Idempotent.

        Jobs must only be added to a *started* scheduler: while it is stopped APScheduler
        holds them in a pending list, where ``replace_existing`` does not apply and job
        defaults are not yet resolved -- so scheduling the same product twice really would
        create two jobs. Starting paused gives a live job store with nothing executing.
        """
        if not self._scheduler.running:
            self._scheduler.start(paused=paused)
            log.info(
                "scheduler.started", paused=paused, jobs=len(self._scheduler.get_jobs())
            )

    def resume(self) -> None:
        if self._scheduler.running:
            self._scheduler.resume()
            log.info("scheduler.resumed", jobs=len(self._scheduler.get_jobs()))

    def shutdown(self, *, wait: bool = True) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            log.info("scheduler.stopped")

    def schedule_product(self, product_id: int, interval_seconds: int) -> None:
        self._scheduler.add_job(
            _run_check,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id=job_id_for(product_id),
            args=[product_id],
            replace_existing=True,
            # Spread first runs out rather than stampeding every product at startup.
            next_run_time=datetime.now(UTC) + timedelta(seconds=_startup_offset(product_id)),
        )
        log.info(
            EVENT_JOB_SCHEDULED, product_id=product_id, interval_seconds=interval_seconds
        )

    def unschedule_product(self, product_id: int) -> None:
        """Remove a product's job. Silent if it was not scheduled -- reconcile may race
        with a product being deleted through the API."""
        job_id = job_id_for(product_id)
        if self._scheduler.get_job(job_id) is not None:
            self._scheduler.remove_job(job_id, jobstore="default")
            log.info("job.unscheduled", product_id=product_id)

    def scheduled_products(self) -> dict[int, int]:
        result: dict[int, int] = {}
        for job in self._scheduler.get_jobs():
            product_id = product_id_from(job.id)
            if product_id is None:
                continue
            interval = getattr(job.trigger, "interval", None)
            if interval is not None:
                result[product_id] = int(interval.total_seconds())
        return result

    def schedule_recurring(
        self,
        job_id: str,
        func: Any,
        interval_seconds: int,
        *,
        first_run_now: bool = False,
    ) -> None:
        # `next_run_time` is only passed when we want to override the trigger. Passing
        # None explicitly does not mean "use the trigger" -- APScheduler reads it as
        # "this job is paused", and the job would silently never run.
        extra: dict[str, Any] = (
            {"next_run_time": datetime.now(UTC)} if first_run_now else {}
        )
        self._scheduler.add_job(
            func,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id=job_id,
            replace_existing=True,
            **extra,
        )
        log.info("job.recurring_scheduled", job_id=job_id, interval_seconds=interval_seconds)


def _startup_offset(product_id: int) -> int:
    """A small, stable per-product delay before the first run.

    Deterministic from the id so a restart does not reshuffle the spread, and bounded so
    nothing waits long. Without it, every product's first check fires simultaneously.
    """
    return product_id % 30


def _run_check(product_id: int) -> None:
    """Module-level entry point for scheduled jobs.

    Must be importable by name: APScheduler pickles a reference to the function into the
    job store, so a bound method or a closure could not survive a restart.
    """
    from ..workers.check_worker import run_check

    run_check(product_id)
