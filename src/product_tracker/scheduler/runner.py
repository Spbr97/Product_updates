"""The worker process.

Runs the schedule, and keeps it matching the database. Products are added and paused
through the API and CLI, which do not talk to the worker; a periodic reconcile is what
closes that gap, so the worker needs no notification channel and no shared memory with
anything else.

Deliberately a separate process from the API. A check can take half a minute and the API
should stay responsive; and either can be restarted without the other.
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
from .apscheduler_queue import APSchedulerJobQueue
from .jobqueue import JobQueue, ReconcileReport
from .throttle import StoreGuard

log = get_logger(__name__)

#: Set by the running WorkerRunner so module-level jobs can reach its queue.
_ACTIVE_QUEUE: JobQueue | None = None


def _set_active_queue(queue: JobQueue | None) -> None:
    global _ACTIVE_QUEUE
    _ACTIVE_QUEUE = queue


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


def reconcile_now(queue: JobQueue, settings: Settings) -> ReconcileReport:
    """One reconcile pass. Safe to call at any time."""
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


class WorkerRunner:
    """Owns the scheduler's lifecycle for one process."""

    def __init__(self, settings: Settings | None = None, queue: JobQueue | None = None) -> None:
        self.settings = settings or get_settings()
        self.guard = StoreGuard(
            min_interval_seconds=self.settings.store_min_interval_seconds,
            jitter_seconds=self.settings.fetch_jitter_seconds,
            failure_threshold=self.settings.store_failure_threshold,
            reset_seconds=self.settings.store_circuit_reset_seconds,
        )
        self.queue = queue or APSchedulerJobQueue(
            self.settings.database_url, check_callable=check_worker.run_check
        )
        self._stopped = threading.Event()

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
        _set_active_queue(self.queue)

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
        return reconcile_now(self.queue, self.settings)

    def run(self) -> None:
        """Start the scheduler and block until stopped. Handles SIGINT and SIGTERM."""
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
        reconcile_now(queue, get_settings())
    except Exception as exc:
        log.error("reconcile.failed", exc_info=exc)
