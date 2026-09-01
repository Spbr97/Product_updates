"""Throttling, circuit breaking, and reconcile logic.

All driven by injected clocks and sleepers, so timing behaviour is asserted exactly rather
than waited for.
"""

from __future__ import annotations

import pytest

from product_tracker.scheduler.jobqueue import (
    JobQueue,
    ReconcileReport,
    job_id_for,
    product_id_from,
)
from product_tracker.scheduler.throttle import StoreGuard


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleeper:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.advance(seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeper(clock: FakeClock) -> RecordingSleeper:
    return RecordingSleeper(clock)


def make_guard(
    clock: FakeClock,
    sleeper: RecordingSleeper,
    *,
    min_interval: float = 5.0,
    jitter: float = 0.0,
    threshold: int = 3,
    reset: float = 900.0,
) -> StoreGuard:
    return StoreGuard(
        min_interval_seconds=min_interval,
        jitter_seconds=jitter,
        failure_threshold=threshold,
        reset_seconds=reset,
        clock=clock,
        sleeper=sleeper,
        jitter=lambda high: high,  # deterministic: always the maximum
    )


class TestThrottle:
    def test_first_request_does_not_wait(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        guard = make_guard(clock, sleeper)

        assert guard.before("flipkart").proceed
        assert sleeper.slept == []

    def test_second_immediate_request_waits_the_interval(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        guard = make_guard(clock, sleeper, min_interval=5.0)
        guard.before("flipkart")

        guard.before("flipkart")

        assert sleeper.slept == [5.0]

    def test_no_wait_once_the_interval_has_passed(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        guard = make_guard(clock, sleeper, min_interval=5.0)
        guard.before("flipkart")
        clock.advance(10)

        guard.before("flipkart")

        assert sleeper.slept == []

    def test_throttling_is_per_store(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        """One busy store must not delay checks for a different one."""
        guard = make_guard(clock, sleeper, min_interval=5.0)
        guard.before("flipkart")

        guard.before("generic")

        assert sleeper.slept == []

    def test_jitter_is_added(self, clock: FakeClock, sleeper: RecordingSleeper) -> None:
        guard = make_guard(clock, sleeper, min_interval=0.0, jitter=3.0)

        guard.before("flipkart")

        assert sleeper.slept == [3.0]

    def test_a_claimed_slot_is_not_handed_out_twice(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        """Two callers in the same instant must be spaced, not both let through."""
        guard = make_guard(clock, sleeper, min_interval=5.0)
        guard.before("flipkart")
        first_wait = sum(sleeper.slept)

        guard.before("flipkart")

        assert sum(sleeper.slept) > first_wait


class TestCircuitBreaker:
    def test_stays_closed_below_the_threshold(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        guard = make_guard(clock, sleeper, threshold=3)

        for _ in range(2):
            guard.after("flipkart", succeeded=False)

        assert guard.before("flipkart").proceed

    def test_opens_at_the_threshold(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        guard = make_guard(clock, sleeper, threshold=3)

        for _ in range(3):
            guard.after("flipkart", succeeded=False)

        decision = guard.before("flipkart")
        assert not decision.proceed
        assert "failed 3 times" in (decision.reason or "")

    def test_success_resets_the_count(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        guard = make_guard(clock, sleeper, threshold=3)
        guard.after("flipkart", succeeded=False)
        guard.after("flipkart", succeeded=False)

        guard.after("flipkart", succeeded=True)
        guard.after("flipkart", succeeded=False)

        assert guard.before("flipkart").proceed

    def test_reopens_after_the_cooling_off_period(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        guard = make_guard(clock, sleeper, threshold=1, reset=900.0)
        guard.after("flipkart", succeeded=False)
        assert not guard.before("flipkart").proceed

        clock.advance(901)

        assert guard.before("flipkart").proceed

    def test_an_open_circuit_is_per_store(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        """The requirement: one failing store must not affect other products."""
        guard = make_guard(clock, sleeper, threshold=1)
        guard.after("flipkart", succeeded=False)

        assert not guard.before("flipkart").proceed
        assert guard.before("generic").proceed

    def test_a_successful_probe_closes_the_circuit(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        guard = make_guard(clock, sleeper, threshold=1, reset=10.0)
        guard.after("flipkart", succeeded=False)
        clock.advance(11)
        guard.before("flipkart")

        guard.after("flipkart", succeeded=True)

        assert guard.before("flipkart").proceed

    def test_snapshot_reports_state(
        self, clock: FakeClock, sleeper: RecordingSleeper
    ) -> None:
        guard = make_guard(clock, sleeper, threshold=1, reset=60.0)
        guard.after("flipkart", succeeded=False)

        snapshot = guard.snapshot()

        assert snapshot["flipkart"]["circuit_open"] is True
        assert snapshot["flipkart"]["consecutive_failures"] == 1


class TestJobIds:
    def test_round_trip(self) -> None:
        assert product_id_from(job_id_for(42)) == 42

    def test_deterministic(self) -> None:
        """Determinism is what makes duplicate jobs impossible."""
        assert job_id_for(42) == job_id_for(42)

    @pytest.mark.parametrize("job_id", ["system:reconcile", "other", "product:abc", ""])
    def test_ignores_jobs_we_did_not_create(self, job_id: str) -> None:
        assert product_id_from(job_id) is None


class FakeQueue(JobQueue):
    """In-memory JobQueue, to test reconcile without APScheduler."""

    def __init__(self, initial: dict[int, int] | None = None) -> None:
        self.jobs: dict[int, int] = dict(initial or {})
        self.recurring: dict[str, int] = {}
        self.started = False
        self.paused = False
        self.schedule_calls = 0

    def start(self, *, paused: bool = False) -> None:
        self.started = True
        self.paused = paused

    def resume(self) -> None:
        self.paused = False

    def shutdown(self, *, wait: bool = True) -> None:
        self.started = False

    def schedule_product(self, product_id: int, interval_seconds: int) -> None:
        self.schedule_calls += 1
        self.jobs[product_id] = interval_seconds

    def unschedule_product(self, product_id: int) -> None:
        self.jobs.pop(product_id, None)

    def scheduled_products(self) -> dict[int, int]:
        return dict(self.jobs)

    def schedule_recurring(
        self, job_id: str, func: object, interval_seconds: int, *, first_run_now: bool = False
    ) -> None:
        self.recurring[job_id] = interval_seconds


class TestReconcile:
    def test_adds_missing_products(self) -> None:
        queue = FakeQueue()

        report = queue.reconcile({1: 3600, 2: 900})

        assert report == ReconcileReport(added=2)
        assert queue.jobs == {1: 3600, 2: 900}

    def test_removes_products_no_longer_wanted(self) -> None:
        """A paused or deleted product disappears from the desired set."""
        queue = FakeQueue({1: 3600, 2: 900})

        report = queue.reconcile({1: 3600})

        assert report.removed == 1
        assert queue.jobs == {1: 3600}

    def test_updates_a_changed_interval(self) -> None:
        queue = FakeQueue({1: 3600})

        report = queue.reconcile({1: 900})

        assert report.updated == 1
        assert queue.jobs == {1: 900}

    def test_leaves_unchanged_products_alone(self) -> None:
        """Rescheduling an unchanged job would reset its next run time every minute."""
        queue = FakeQueue({1: 3600})

        report = queue.reconcile({1: 3600})

        assert report == ReconcileReport(unchanged=1)
        assert queue.schedule_calls == 0

    def test_is_idempotent(self) -> None:
        queue = FakeQueue()
        desired = {1: 3600, 2: 900}

        queue.reconcile(desired)
        second = queue.reconcile(desired)

        assert not second.changed
        assert queue.jobs == desired

    def test_reconciling_to_empty_removes_everything(self) -> None:
        queue = FakeQueue({1: 3600, 2: 900})

        report = queue.reconcile({})

        assert report.removed == 2
        assert queue.jobs == {}

    def test_report_renders_readably(self) -> None:
        assert "added=1" in str(ReconcileReport(added=1))
