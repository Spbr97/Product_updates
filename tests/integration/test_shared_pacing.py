"""Pacing that holds across processes, not just within one.

The bug being fixed: throttle state lived in a dict in memory, so two people searching at
the same moment were two independent rate limiters and each believed it was the only
caller. Probing one retailer too hard is what got a shop to stop answering this machine, so
these tests are the difference between a guard and the appearance of one.

They use a real database on purpose. A guard that coordinates through PostgreSQL cannot be
tested against a fake that has no notion of a row lock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from itertools import pairwise

import pytest
from sqlalchemy import text

from product_tracker.scheduler.throttle import SharedStoreGuard

pytestmark = pytest.mark.db

HOST = "shop.test"


@pytest.fixture(autouse=True)
def _clean_pacing(db_env: None) -> Iterator[None]:
    from product_tracker.db.session import get_engine

    def wipe() -> None:
        with get_engine().begin() as connection:
            connection.execute(text("DELETE FROM store_pacing"))

    wipe()
    yield
    wipe()


def build(
    *, interval: float = 0.4, max_wait: float = 20.0, threshold: int = 3, reset: float = 30.0
) -> SharedStoreGuard:
    """A guard that records its waits instead of serving them.

    The wait is what is under test; sleeping it would only make the suite slow, and the
    claim has already been written to the database by the time it is returned.
    """
    return SharedStoreGuard(
        min_interval_seconds=interval,
        jitter_seconds=0.0,
        failure_threshold=threshold,
        reset_seconds=reset,
        max_wait_seconds=max_wait,
        sleeper=lambda _seconds: None,
        jitter=lambda _high: 0.0,
    )


class TestSlotsAreQueued:
    def test_the_first_caller_waits_for_nothing(self) -> None:
        assert build().before(HOST).proceed

    def test_consecutive_callers_are_spaced(self) -> None:
        """Each claim pushes the next slot further out."""
        guard = build(interval=0.4)
        waits = [guard._claim(HOST)[0] for _ in range(4)]

        assert waits[0] == pytest.approx(0.0, abs=0.05)
        # 0.4s, 0.8s, 1.2s -- each caller queues behind the one before it.
        for index, wait in enumerate(waits[1:], start=1):
            assert wait == pytest.approx(0.4 * index, abs=0.15)

    def test_separate_guard_objects_share_the_queue(self) -> None:
        """The heart of it.

        Two guards with no memory of each other stand in for two processes. Before this
        they both saw an empty dict and both fired immediately.
        """
        first, second = build(interval=0.5), build(interval=0.5)

        assert first._claim(HOST)[0] == pytest.approx(0.0, abs=0.05)
        assert second._claim(HOST)[0] == pytest.approx(0.5, abs=0.15)

    def test_different_hosts_do_not_queue_behind_each_other(self) -> None:
        """Politeness is owed to a server, so one busy shop must not delay another."""
        guard = build(interval=0.5)
        guard._claim("busy.test")
        guard._claim("busy.test")

        assert guard._claim("quiet.test")[0] == pytest.approx(0.0, abs=0.05)

    def test_an_idle_host_does_not_accumulate_credit(self) -> None:
        """greatest(now(), next_allowed_at): a host nobody asked about for an hour is due
        one slot, not thirty-six hundred."""
        guard = build(interval=0.3)
        guard._claim(HOST)
        time.sleep(0.45)

        assert guard._claim(HOST)[0] == pytest.approx(0.0, abs=0.1)


class TestConcurrentCallers:
    def test_threads_are_given_distinct_slots(self) -> None:
        """Ten callers at once get ten different turns, not ten simultaneous ones."""
        guard = build(interval=0.25)
        waits: list[float] = []
        lock = threading.Lock()

        def claim() -> None:
            wait, refusal = guard._claim(HOST)
            assert refusal is None
            with lock:
                waits.append(wait)

        threads = [threading.Thread(target=claim) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        ordered = sorted(waits)
        assert len(ordered) == 10
        # No two callers were told to go at the same moment.
        for earlier, later in pairwise(ordered):
            assert later - earlier >= 0.15
        # And the last one waits roughly nine gaps, not none.
        assert ordered[-1] == pytest.approx(0.25 * 9, abs=0.5)


class TestRefusingRatherThanQueueing:
    def test_a_wait_beyond_the_cap_is_refused(self) -> None:
        """Ten people searching at once should not each wait a minute for the tenth slot."""
        guard = build(interval=1.0, max_wait=2.0)
        for _ in range(4):
            guard._claim(HOST)

        _wait, refusal = guard._claim(HOST)

        assert refusal is not None
        assert not refusal.proceed
        assert "busy" in (refusal.reason or "")

    def test_the_refusal_says_how_long_the_queue_is(self) -> None:
        guard = build(interval=1.0, max_wait=2.0)
        for _ in range(5):
            guard._claim(HOST)

        _wait, refusal = guard._claim(HOST)
        assert refusal is not None
        assert "s away" in (refusal.reason or "")


class TestCircuitAcrossProcesses:
    def test_failures_recorded_by_one_guard_open_the_circuit_for_another(self) -> None:
        """A shop refusing everybody should be left alone by every process at once, not
        rediscovered separately by each one."""
        first, second = build(threshold=3), build(threshold=3)
        for _ in range(3):
            first.after(HOST, succeeded=False)

        decision = second.before(HOST)

        assert not decision.proceed
        assert "failed 3 times" in (decision.reason or "")

    def test_success_clears_it_for_everyone(self) -> None:
        first, second = build(threshold=2), build(threshold=2)
        for _ in range(2):
            first.after(HOST, succeeded=False)
        assert not second.before(HOST).proceed

        second.after(HOST, succeeded=True)

        assert build(threshold=2).before(HOST).proceed

    def test_the_circuit_reopens_after_its_cooling_off(self) -> None:
        guard = build(threshold=1, reset=0.4)
        guard.after(HOST, succeeded=False)
        assert not guard.before(HOST).proceed

        time.sleep(0.5)

        assert guard.before(HOST).proceed


class TestSnapshot:
    def test_reports_what_every_process_can_see(self) -> None:
        guard = build(threshold=2)
        guard.after(HOST, succeeded=False)
        guard.after(HOST, succeeded=False)

        state = build().snapshot()[HOST]

        assert state["consecutive_failures"] == 2
        assert state["circuit_open"] is True
        assert int(state["opens_for_seconds"]) > 0  # type: ignore[call-overload]

    def test_a_healthy_host_is_not_reported_as_open(self) -> None:
        guard = build()
        guard.before(HOST)
        guard.after(HOST, succeeded=True)

        assert guard.snapshot()[HOST]["circuit_open"] is False
