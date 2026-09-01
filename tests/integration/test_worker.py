"""Retries, guard behaviour, and the real APScheduler-backed job queue."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from sqlalchemy.orm import Session
from tests.unit.test_adapters import load
from tests.unit.test_scheduler import FakeClock, FakeQueue, RecordingSleeper, make_guard

from product_tracker.core.config import get_settings
from product_tracker.domain.enums import CheckStatus
from product_tracker.scheduler.apscheduler_queue import APSchedulerJobQueue
from product_tracker.scheduler.jobqueue import job_id_for
from product_tracker.scheduler.runner import (
    NOTIFICATION_RETRY_JOB_ID,
    RECONCILE_JOB_ID,
    WorkerRunner,
    desired_schedule,
    reconcile_now,
)
from product_tracker.services.product_service import ProductService
from product_tracker.services.tracking import TrackingEngine
from product_tracker.stores.registry import StoreRegistry
from product_tracker.workers import check_worker

pytestmark = pytest.mark.db

URL = "https://shop.example.com/p/worker"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture
def service(db_session: Session) -> ProductService:
    return ProductService(db_session, StoreRegistry(), get_settings())


class NoSleep:
    """Records requested delays without actually waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class TestRetry:
    def test_a_transient_failure_is_retried(
        self, service: ProductService, db_session: Session
    ) -> None:
        sleeper = NoSleep()
        engine = TrackingEngine(StoreRegistry(), get_settings(), sleeper=sleeper)
        route = respx.get(URL)
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, html=load("jsonld_in_stock.html")),
        ]
        product = service.add(URL)

        execution = engine.check_product(db_session, product.id)

        assert execution.status is CheckStatus.SUCCESS
        assert execution.attempts == 3
        assert len(sleeper.delays) == 2

    def test_backoff_grows(self, service: ProductService, db_session: Session) -> None:
        sleeper = NoSleep()
        engine = TrackingEngine(StoreRegistry(), get_settings(), sleeper=sleeper)
        respx.get(URL).mock(return_value=httpx.Response(503))
        product = service.add(URL)

        engine.check_product(db_session, product.id)

        assert sleeper.delays[1] > sleeper.delays[0]

    def test_retries_are_bounded(
        self, service: ProductService, db_session: Session
    ) -> None:
        sleeper = NoSleep()
        settings = get_settings()
        engine = TrackingEngine(StoreRegistry(), settings, sleeper=sleeper)
        respx.get(URL).mock(return_value=httpx.Response(503))
        product = service.add(URL)

        execution = engine.check_product(db_session, product.id)

        assert execution.status is CheckStatus.FAILED
        assert execution.attempts == settings.http_max_retries

    def test_a_block_is_not_retried(
        self, service: ProductService, db_session: Session
    ) -> None:
        """Retrying a block is exactly what the store is objecting to."""
        sleeper = NoSleep()
        engine = TrackingEngine(StoreRegistry(), get_settings(), sleeper=sleeper)
        respx.get(URL).mock(return_value=httpx.Response(403))
        product = service.add(URL)

        execution = engine.check_product(db_session, product.id)

        assert execution.attempts == 1
        assert sleeper.delays == []

    def test_a_missing_price_is_not_retried(
        self, service: ProductService, db_session: Session
    ) -> None:
        """An unparseable page will not parse differently a second later."""
        sleeper = NoSleep()
        engine = TrackingEngine(StoreRegistry(), get_settings(), sleeper=sleeper)
        respx.get(URL).mock(return_value=httpx.Response(200, html=load("no_product.html")))
        product = service.add(URL)

        execution = engine.check_product(db_session, product.id)

        assert execution.attempts == 1
        assert sleeper.delays == []

    def test_success_records_a_single_attempt(
        self, service: ProductService, db_session: Session
    ) -> None:
        engine = TrackingEngine(StoreRegistry(), get_settings(), sleeper=NoSleep())
        respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        product = service.add(URL)

        assert engine.check_product(db_session, product.id).attempts == 1


class TestGuardIntegration:
    """The guard keys on the host; these seed it with the host under test."""

    def test_an_open_circuit_skips_the_check(
        self, service: ProductService, db_session: Session
    ) -> None:
        clock = FakeClock()
        guard = make_guard(clock, RecordingSleeper(clock), threshold=1)
        guard.after("shop.example.com", succeeded=False)
        engine = TrackingEngine(StoreRegistry(), get_settings(), guard=guard)
        product = service.add(URL)

        execution = engine.check_product(db_session, product.id)

        assert execution.status is CheckStatus.SKIPPED
        assert execution.error_type == "skipped_by_guard"
        assert "backing off" in (execution.error_detail or "")

    def test_a_skip_makes_no_request(
        self, service: ProductService, db_session: Session
    ) -> None:
        clock = FakeClock()
        guard = make_guard(clock, RecordingSleeper(clock), threshold=1)
        guard.after("shop.example.com", succeeded=False)
        engine = TrackingEngine(StoreRegistry(), get_settings(), guard=guard)
        route = respx.get(URL).mock(return_value=httpx.Response(200))
        product = service.add(URL)

        engine.check_product(db_session, product.id)

        assert not route.called

    def test_a_skip_is_not_counted_as_a_failure(
        self, service: ProductService, db_session: Session
    ) -> None:
        """Nothing was attempted, so the product's failure streak must not grow."""
        clock = FakeClock()
        guard = make_guard(clock, RecordingSleeper(clock), threshold=1)
        guard.after("shop.example.com", succeeded=False)
        engine = TrackingEngine(StoreRegistry(), get_settings(), guard=guard)
        product = service.add(URL)

        engine.check_product(db_session, product.id)

        assert product.consecutive_failures == 0
        assert product.last_checked_at is None

    def test_the_guard_learns_from_the_outcome(
        self, service: ProductService, db_session: Session
    ) -> None:
        clock = FakeClock()
        guard = make_guard(clock, RecordingSleeper(clock), threshold=1)
        engine = TrackingEngine(StoreRegistry(), get_settings(), guard=guard, sleeper=NoSleep())
        respx.get(URL).mock(return_value=httpx.Response(403))
        product = service.add(URL)

        engine.check_product(db_session, product.id)

        assert guard.snapshot()["shop.example.com"]["circuit_open"] is True

    def test_no_guard_means_no_skipping(
        self, service: ProductService, db_session: Session
    ) -> None:
        """A one-shot CLI check has no pacing to do."""
        engine = TrackingEngine(StoreRegistry(), get_settings())
        respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        product = service.add(URL)

        assert engine.check_product(db_session, product.id).status is CheckStatus.SUCCESS


def add_committed(url: str = URL, *, interval: int | None = None) -> int:
    """Create a product in its own committed transaction.

    ``desired_schedule`` opens a separate connection, so it cannot see anything held in
    the rollback-wrapped ``db_session``.
    """
    from product_tracker.db.session import session_scope

    with session_scope() as session:
        service = ProductService(session, StoreRegistry(), get_settings())
        product = service.add(url, check_interval_seconds=interval)
        return int(product.id)


class TestDesiredSchedule:
    def test_uses_the_per_product_interval(self, clean_db: None) -> None:
        product_id = add_committed(interval=900)

        assert desired_schedule(get_settings())[product_id] == 900

    def test_falls_back_to_the_global_default(self, clean_db: None) -> None:
        product_id = add_committed()
        settings = get_settings()

        assert desired_schedule(settings)[product_id] == settings.check_interval_seconds

    def test_paused_products_are_excluded(self, clean_db: None) -> None:
        from product_tracker.db.session import session_scope
        from product_tracker.domain.enums import TrackingStatus
        from product_tracker.services.alert_service import AlertService
        from product_tracker.services.user_service import default_user

        product_id = add_committed()
        with session_scope() as session:
            service = AlertService(session, default_user(session).id)
            service.set_tracking_status(product_id, TrackingStatus.PAUSED)

        assert product_id not in desired_schedule(get_settings())


class TestRunnerSetup:
    def test_installs_housekeeping_jobs_and_reconciles(self, clean_db: None) -> None:
        product_id = add_committed()
        queue = FakeQueue()
        runner = WorkerRunner(get_settings(), queue=queue)

        report = runner.setup()

        assert RECONCILE_JOB_ID in queue.recurring
        assert NOTIFICATION_RETRY_JOB_ID in queue.recurring
        assert report.added == 1
        assert product_id in queue.jobs
        runner.stop()

    def test_setup_starts_the_queue_paused(self, clean_db: None) -> None:
        """Nothing may execute until the whole schedule is installed."""
        queue = FakeQueue()
        runner = WorkerRunner(get_settings(), queue=queue)

        runner.setup()

        assert queue.started is True
        assert queue.paused is True
        runner.stop()

    def test_reconcile_picks_up_a_new_product(self, clean_db: None) -> None:
        """Products are added through the API, which never talks to the worker."""
        queue = FakeQueue()
        settings = get_settings()
        reconcile_now(queue, settings)

        product_id = add_committed()
        report = reconcile_now(queue, settings)

        assert report.added == 1
        assert product_id in queue.jobs


class TestAPSchedulerQueue:
    """Against the real APScheduler with the PostgreSQL job store."""

    @pytest.fixture
    def queue(self, db_env: None) -> Iterator[APSchedulerJobQueue]:
        settings = get_settings()
        instance = APSchedulerJobQueue(
            settings.database_url, check_callable=lambda _pid: None
        )
        # Paused: a live job store, with nothing executing during the test.
        instance.start(paused=True)
        yield instance
        for job in instance.scheduler.get_jobs():
            instance.scheduler.remove_job(job.id)
        instance.shutdown(wait=False)

    def test_scheduling_persists_to_postgres(self, queue: APSchedulerJobQueue) -> None:
        queue.schedule_product(1, 3600)

        assert queue.scheduled_products() == {1: 3600}

    def test_scheduling_twice_does_not_duplicate(
        self, queue: APSchedulerJobQueue
    ) -> None:
        """The whole point of a deterministic job id."""
        queue.schedule_product(1, 3600)
        queue.schedule_product(1, 3600)

        assert len(queue.scheduler.get_jobs()) == 1
        assert queue.scheduler.get_job(job_id_for(1)) is not None

    def test_rescheduling_changes_the_interval(
        self, queue: APSchedulerJobQueue
    ) -> None:
        queue.schedule_product(1, 3600)
        queue.schedule_product(1, 900)

        assert queue.scheduled_products() == {1: 900}

    def test_unschedule_removes_the_job(self, queue: APSchedulerJobQueue) -> None:
        queue.schedule_product(1, 3600)

        queue.unschedule_product(1)

        assert queue.scheduled_products() == {}

    def test_unscheduling_something_absent_is_silent(
        self, queue: APSchedulerJobQueue
    ) -> None:
        """Reconcile can race with a product being deleted through the API."""
        queue.unschedule_product(999)

    def test_housekeeping_jobs_are_not_mistaken_for_products(
        self, queue: APSchedulerJobQueue
    ) -> None:
        """A module-level function, not a lambda: the job store pickles a reference."""
        queue.schedule_recurring(
            RECONCILE_JOB_ID, check_worker.retry_notifications, 60
        )
        queue.schedule_product(1, 3600)

        assert queue.scheduled_products() == {1: 3600}
        assert queue.scheduler.get_job(RECONCILE_JOB_ID) is not None

    def test_reconcile_against_the_real_store(
        self, queue: APSchedulerJobQueue
    ) -> None:
        queue.schedule_product(1, 3600)
        queue.schedule_product(2, 3600)

        report = queue.reconcile({1: 3600, 3: 900})

        assert (report.added, report.removed, report.unchanged) == (1, 1, 1)
        assert queue.scheduled_products() == {1: 3600, 3: 900}

    def test_job_defaults_prevent_pile_up(self, queue: APSchedulerJobQueue) -> None:
        """A slow check must not get a second copy started on top of it."""
        queue.schedule_product(1, 60)
        job = queue.scheduler.get_job(job_id_for(1))

        assert job.max_instances == 1
        assert job.coalesce is True

    def test_recurring_jobs_are_actually_armed(self, queue: APSchedulerJobQueue) -> None:
        """A job with no next_run_time is *paused* in APScheduler, not merely unscheduled.

        Passing ``next_run_time=None`` explicitly created the reconcile and
        notification-retry jobs in a paused state, so they never ran and the worker never
        noticed products added after startup.
        """
        queue.schedule_recurring(
            RECONCILE_JOB_ID, check_worker.retry_notifications, 60
        )

        job = queue.scheduler.get_job(RECONCILE_JOB_ID)
        assert job is not None
        assert job.next_run_time is not None

    def test_first_run_now_is_honoured(self, queue: APSchedulerJobQueue) -> None:
        queue.schedule_recurring(
            NOTIFICATION_RETRY_JOB_ID,
            check_worker.retry_notifications,
            600,
            first_run_now=True,
        )

        job = queue.scheduler.get_job(NOTIFICATION_RETRY_JOB_ID)
        assert job is not None
        assert job.next_run_time is not None


class TestGuardIsolatesHosts:
    """Several real retailers share the generic adapter.

    Found by running against five live Indian retailers at once: Croma hard-blocks
    automated access, and because the guard keyed on the *adapter slug*, its failures
    opened a circuit under "generic" -- which would have skipped Vijay Sales, BigBasket
    and Reliance Digital too. The guard keys on the host now.
    """

    def _engine(self, guard: object) -> TrackingEngine:
        return TrackingEngine(
            StoreRegistry(), get_settings(), guard=guard, sleeper=NoSleep()
        )

    def test_a_blocked_host_does_not_skip_a_healthy_one(
        self, service: ProductService, db_session: Session
    ) -> None:
        clock = FakeClock()
        guard = make_guard(clock, RecordingSleeper(clock), threshold=1, min_interval=0)
        engine = self._engine(guard)

        blocked_url = "https://blocked.example.com/p/1"
        healthy_url = "https://healthy.example.com/p/1"
        respx.get(blocked_url).mock(return_value=httpx.Response(403))
        respx.get(healthy_url).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )
        blocked = service.add(blocked_url)
        healthy = service.add(healthy_url)
        # Both resolve to the same adapter -- that is the whole point.
        assert blocked.store.slug == healthy.store.slug == "generic"

        engine.check_product(db_session, blocked.id)  # opens the circuit for its host
        assert not guard.before("blocked.example.com").proceed

        execution = engine.check_product(db_session, healthy.id)

        assert execution.status is CheckStatus.SUCCESS

    def test_the_circuit_is_recorded_against_the_host(
        self, service: ProductService, db_session: Session
    ) -> None:
        clock = FakeClock()
        guard = make_guard(clock, RecordingSleeper(clock), threshold=1, min_interval=0)
        url = "https://blocked.example.com/p/2"
        respx.get(url).mock(return_value=httpx.Response(403))
        product = service.add(url)

        self._engine(guard).check_product(db_session, product.id)

        snapshot = guard.snapshot()
        assert "blocked.example.com" in snapshot
        assert "generic" not in snapshot

    def test_throttling_is_per_host_not_per_adapter(
        self, service: ProductService, db_session: Session
    ) -> None:
        """Four unrelated retailers must not queue behind one 5-second bucket."""
        clock = FakeClock()
        sleeper = RecordingSleeper(clock)
        guard = make_guard(clock, sleeper, min_interval=5.0, threshold=99)
        engine = self._engine(guard)

        for host in ("one.example.com", "two.example.com", "three.example.com"):
            url = f"https://{host}/p/1"
            respx.get(url).mock(
                return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
            )
            engine.check_product(db_session, service.add(url).id)

        assert sleeper.slept == []
