"""The worker process.

Runs the schedule, and keeps it matching the database. Products are added and paused
through the API and CLI, which do not talk to the worker; a periodic reconcile is what
closes that gap, so the worker needs no notification channel and no shared memory with
anything else.

Deliberately a separate process from the API. A check can take half a minute and the API
should stay responsive; and either can be restarted without the other.

Exactly one worker may run per database, enforced by an advisory lock rather than left to
documentation -- see ``lock.py``.
"""

from __future__ import annotations

import signal
import threading
from types import FrameType

from ..core.config import Settings, get_settings
from ..core.logging import EVENT_JOB_RECONCILED, get_logger
from ..db.session import session_scope
from ..repositories.products import ProductRepository
from ..workers import check_worker
from . import heartbeat
from .apscheduler_queue import APSchedulerJobQueue
from .jobqueue import JobQueue, ReconcileReport
from .lock import WorkerLock
from .throttle import build_guard

log = get_logger(__name__)

#: Set by the running WorkerRunner so module-level jobs can reach its queue.
_ACTIVE_QUEUE: JobQueue | None = None
_ACTIVE_WORKER_ID: str | None = None


def _set_active_queue(queue: JobQueue | None, worker_id: str | None = None) -> None:
    global _ACTIVE_QUEUE, _ACTIVE_WORKER_ID
    _ACTIVE_QUEUE = queue
    _ACTIVE_WORKER_ID = worker_id


RECONCILE_JOB_ID = "system:reconcile"
NOTIFICATION_RETRY_JOB_ID = "system:notification-retry"

#: How often to retry notifications that failed delivery. Rarer than reconcile: a failing
#: provider is usually failing for a while.
NOTIFICATION_RETRY_SECONDS = 300


def desired_schedule(settings: Settings) -> dict[int, int]:
    """``{product_id: interval_seconds}`` for every product that should be checked.

    Paused products are simply absent, which is what makes reconcile remove their jobs.
    """
    with session_scope() as session:
        products = ProductRepository(session).list_schedulable()
        return {
            product.id: product.check_interval_seconds or settings.check_interval_seconds
            for product in products
        }


def reconcile_now(
    queue: JobQueue, settings: Settings, *, worker_id: str | None = None
) -> ReconcileReport:
    """One reconcile pass, and a heartbeat. Safe to call at any time."""
    if worker_id is not None:
        _beat(worker_id, settings)
    report = queue.reconcile(desired_schedule(settings))
    if report.changed:
        log.info(
            EVENT_JOB_RECONCILED,
            added=report.added,
            updated=report.updated,
            removed=report.removed,
            unchanged=report.unchanged,
        )
    return report


def _beat(worker_id: str, settings: Settings) -> None:
    """Record liveness, and tidy up after workers that died without cleaning up.

    Never raises: a heartbeat failure must not stop the checks the worker exists to run.
    """
    try:
        with session_scope() as session:
            heartbeat.touch(session, worker_id)
            heartbeat.prune(
                session,
                older_than_seconds=heartbeat.stale_after(settings.reconcile_interval_seconds) * 10,
            )
    except Exception as exc:
        log.warning("heartbeat.failed", exc_info=exc)


class WorkerRunner:
    """Owns the scheduler's lifecycle for one process."""

    def __init__(self, settings: Settings | None = None, queue: JobQueue | None = None) -> None:
        self.settings = settings or get_settings()
        self.worker_id = heartbeat.new_worker_id()
        self.guard = build_guard(self.settings)
        self.queue = queue or APSchedulerJobQueue(
            self.settings.database_url, check_callable=check_worker.run_check
        )
        self._stopped = threading.Event()
        self.lock = WorkerLock()

    def setup(self) -> ReconcileReport:
        """Install the housekeeping jobs and bring the schedule up to date.

        Starts the queue paused first: jobs added to a stopped scheduler would not be
        deduplicated by their id, and nothing should execute until the whole schedule is
        consistent.
        """
        self.queue.start(paused=True)
        check_worker.set_guard(self.guard)
        # The reconcile job is a module-level function (APScheduler pickles a reference
        # to it), so it reaches this runner's queue through module state.
        _set_active_queue(self.queue, self.worker_id)

        self.queue.schedule_recurring(
            RECONCILE_JOB_ID,
            _reconcile_job,
            self.settings.reconcile_interval_seconds,
            first_run_now=False,
        )
        self.queue.schedule_recurring(
            NOTIFICATION_RETRY_JOB_ID,
            check_worker.retry_notifications,
            NOTIFICATION_RETRY_SECONDS,
            first_run_now=False,
        )
        return reconcile_now(self.queue, self.settings, worker_id=self.worker_id)

    def run(self) -> None:
        """Start the scheduler and block until stopped. Handles SIGINT and SIGTERM.

        Takes an advisory lock first: a second worker against the same database would run
        every job a second time, and refusing loudly is better than doubling silently.
        Raises ``WorkerAlreadyRunningError`` if another worker holds it.
        """
        with self.lock:
            self._run_locked()

    def _run_locked(self) -> None:
        report = self.setup()
        log.info(
            "worker.starting",
            products=report.added + report.unchanged + report.updated,
            reconcile_seconds=self.settings.reconcile_interval_seconds,
            default_interval_seconds=self.settings.check_interval_seconds,
        )

        self._install_signal_handlers()
        # The schedule is installed and consistent; let it run.
        self.queue.resume()
        try:
            # Wait rather than spin: the scheduler runs its own threads.
            self._stopped.wait()
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop accepting work and let running checks finish."""
        self._stopped.set()
        self.queue.shutdown(wait=True)
        check_worker.set_guard(None)
        _set_active_queue(None)
        # Clear the heartbeat so a deliberately stopped worker reads as stopped at once,
        # rather than "running" until the beat goes stale.
        try:
            with session_scope() as session:
                heartbeat.clear(session, self.worker_id)
        except Exception as exc:
            log.warning("heartbeat.clear_failed", exc_info=exc)

    def _install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: FrameType | None) -> None:
            log.info("worker.signal", signal=signal.Signals(signum).name)
            self._stopped.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except ValueError:
                # Not on the main thread (embedded, or under a test runner). The caller
                # is then responsible for calling stop().
                log.debug("worker.no_signal_handler", signal=sig)


def _reconcile_job() -> None:
    """The periodic reconcile. Module-level so APScheduler can pickle a reference to it.

    Never raises: a reconcile failure must not stop the scheduler running the checks it
    already knows about.
    """
    queue = _ACTIVE_QUEUE
    if queue is None:
        return
    try:
        reconcile_now(queue, get_settings(), worker_id=_ACTIVE_WORKER_ID)
    except Exception as exc:
        log.error("reconcile.failed", exc_info=exc)
