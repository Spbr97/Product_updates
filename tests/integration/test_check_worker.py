"""The scheduled-job entry point, the runner lifecycle, and scheduler status.

``run_check`` has one hard contract: it must never raise. Anything escaping it reaches
APScheduler's executor, which logs and moves on -- leaving the failure invisible in our own
data. These tests push every failure mode through it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.unit.test_adapters import load
from tests.unit.test_scheduler import FakeClock, FakeQueue, RecordingSleeper, make_guard

from product_tracker.core.config import get_settings
from product_tracker.db.session import get_engine, session_scope
from product_tracker.repositories.executions import (
    MAX_ERROR_DETAIL,
    CheckExecutionRepository,
    truncate_error,
)
from product_tracker.scheduler.runner import WorkerRunner
from product_tracker.scheduler.status import OVERDUE_SECONDS, scheduler_status
from product_tracker.services.product_service import ProductService
from product_tracker.stores.registry import StoreRegistry
from product_tracker.workers import check_worker

pytestmark = pytest.mark.db

URL = "https://shop.example.com/p/job"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture(autouse=True)
def _clear_guard() -> Iterator[None]:
    """The worker's guard is module state; never leak it between tests."""
    yield
    check_worker.set_guard(None)


def add_committed(url: str = URL) -> int:
    with session_scope() as session:
        product = ProductService(session, StoreRegistry(), get_settings()).add(url)
        return int(product.id)


class TestRunCheckNeverRaises:
    def test_a_successful_check_records_an_execution(self, clean_db: None) -> None:
        respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        product_id = add_committed()

        check_worker.run_check(product_id)

        with session_scope() as session:
            executions = CheckExecutionRepository(session).list_for_product(product_id)
        assert len(executions) == 1
        assert executions[0].status.value == "success"

    def test_a_deleted_product_is_not_an_error(self, clean_db: None) -> None:
        """Reconcile can race with a deletion; the next pass removes the job."""
        check_worker.run_check(999_999)

    def test_a_store_failure_is_recorded_not_raised(self, clean_db: None) -> None:
        respx.get(URL).mock(return_value=httpx.Response(403))
        product_id = add_committed()

        check_worker.run_check(product_id)

        with session_scope() as session:
            executions = CheckExecutionRepository(session).list_for_product(product_id)
        assert executions[0].status.value == "failed"

    def test_an_unexpected_error_does_not_escape(
        self, clean_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One product's bug must not stop the scheduler checking every other product."""
        product_id = add_committed()

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("something entirely unexpected")

        monkeypatch.setattr(check_worker, "build_engine", explode)

        check_worker.run_check(product_id)  # must not raise

    def test_a_database_failure_does_not_escape(
        self, clean_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode() -> None:
            raise OSError("database is gone")

        monkeypatch.setattr(check_worker, "session_scope", explode)

        check_worker.run_check(1)  # must not raise

    def test_the_shared_guard_is_used(self, clean_db: None) -> None:
        """All jobs must pace against the same state, not one guard each."""
        clock = FakeClock()
        guard = make_guard(clock, RecordingSleeper(clock), threshold=1)
        # The guard keys on the host, not the adapter slug.
        guard.after("shop.example.com", succeeded=False)  # open the circuit
        check_worker.set_guard(guard)
        product_id = add_committed()

        check_worker.run_check(product_id)

        with session_scope() as session:
            executions = CheckExecutionRepository(session).list_for_product(product_id)
        assert executions[0].status.value == "skipped"


class TestRetryNotifications:
    def test_runs_without_pending_work(self, clean_db: None) -> None:
        check_worker.retry_notifications()

    def test_a_failure_does_not_escape(
        self, clean_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode() -> None:
            raise OSError("database is gone")

        monkeypatch.setattr(check_worker, "session_scope", explode)

        check_worker.retry_notifications()  # must not raise


class TestRunnerLifecycle:
    def test_stop_is_safe_before_start(self, clean_db: None) -> None:
        runner = WorkerRunner(get_settings(), queue=FakeQueue())

        runner.stop()

        assert runner.queue.started is False

    def test_setup_then_stop_clears_the_guard(self, clean_db: None) -> None:
        """Module state must not outlive the runner that set it."""
        runner = WorkerRunner(get_settings(), queue=FakeQueue())
        runner.setup()
        assert check_worker._GUARD is not None

        runner.stop()

        assert check_worker._GUARD is None

    def test_setup_is_idempotent(self, clean_db: None) -> None:
        queue = FakeQueue()
        runner = WorkerRunner(get_settings(), queue=queue)

        runner.setup()
        second = runner.setup()

        assert not second.changed
        runner.stop()

    def test_the_reconcile_job_tolerates_no_active_queue(self, clean_db: None) -> None:
        from product_tracker.scheduler.runner import _reconcile_job, _set_active_queue

        _set_active_queue(None)

        _reconcile_job()  # must not raise


class TestSchedulerStatus:
    def _clear_jobs(self) -> None:
        with get_engine().begin() as connection:
            connection.execute(text("DELETE FROM apscheduler_jobs"))

    def _insert_job(self, job_id: str, next_run: float | None) -> None:
        with get_engine().begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO apscheduler_jobs (id, next_run_time, job_state) "
                    "VALUES (:id, :next_run, :state)"
                ),
                {"id": job_id, "next_run": next_run, "state": b"x"},
            )

    def test_no_jobs(self, db_env: None) -> None:
        self._clear_jobs()
        with session_scope() as session:
            state = scheduler_status(session)

        assert state.available
        assert state.total_jobs == 0
        assert state.worker_running is None

    def test_counts_only_product_jobs(self, db_env: None) -> None:
        self._clear_jobs()
        soon = (datetime.now(UTC) + timedelta(seconds=30)).timestamp()
        self._insert_job("product:1", soon)
        self._insert_job("system:reconcile", soon)

        with session_scope() as session:
            state = scheduler_status(session)

        assert state.total_jobs == 2
        assert state.product_jobs == 1

    def test_a_due_job_means_the_worker_looks_alive(self, db_env: None) -> None:
        self._clear_jobs()
        self._insert_job("product:1", (datetime.now(UTC) + timedelta(seconds=30)).timestamp())

        with session_scope() as session:
            state = scheduler_status(session)

        assert state.worker_running is True

    def test_a_long_overdue_job_means_nothing_is_running_it(self, db_env: None) -> None:
        self._clear_jobs()
        overdue = (datetime.now(UTC) - timedelta(seconds=OVERDUE_SECONDS + 60)).timestamp()
        self._insert_job("product:1", overdue)

        with session_scope() as session:
            state = scheduler_status(session)

        assert state.worker_running is False
        assert "no worker appears to be running" in state.detail

    def test_paused_jobs_do_not_count_as_overdue(self, db_env: None) -> None:
        """A job with no next_run_time is paused, not late."""
        self._clear_jobs()
        self._insert_job("product:1", None)

        with session_scope() as session:
            state = scheduler_status(session)

        assert "paused" in state.detail
        assert state.next_run_at is None

    def test_a_missing_table_is_reported_not_raised(self, db_env: None) -> None:
        """Before migrations have run, status must explain itself rather than crash.

        Done inside a transaction that is rolled back: actually dropping the table would
        take its index with it and break the migration teardown for the whole session.
        """
        connection = get_engine().connect()
        transaction = connection.begin()
        try:
            connection.execute(text("DROP TABLE apscheduler_jobs"))
            state = scheduler_status(Session(bind=connection))
        finally:
            transaction.rollback()
            connection.close()

        assert not state.available
        assert "alembic upgrade" in state.detail


class TestErrorTruncation:
    def test_short_messages_pass_through(self) -> None:
        assert truncate_error("blocked") == "blocked"

    def test_long_messages_are_clamped(self) -> None:
        result = truncate_error("x" * (MAX_ERROR_DETAIL * 2))

        assert result is not None
        assert len(result) == MAX_ERROR_DETAIL
        assert result.endswith("...")

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_becomes_none(self, value: str | None) -> None:
        assert truncate_error(value) is None


class TestExecutionRepository:
    def test_latest_is_the_most_recent(self, clean_db: None) -> None:
        respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        product_id = add_committed()
        check_worker.run_check(product_id)
        check_worker.run_check(product_id)

        with session_scope() as session:
            repo = CheckExecutionRepository(session)
            latest = repo.latest_for_product(product_id)
            all_rows = repo.list_for_product(product_id)

        assert latest is not None
        assert latest.id == max(row.id for row in all_rows)

    def test_latest_is_none_without_checks(self, db_session: Session) -> None:
        assert CheckExecutionRepository(db_session).latest_for_product(999_999) is None

