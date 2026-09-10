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
from datetime import datetime

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


def read_slot() -> datetime:
    """The shared row's next free slot, in the database's own clock."""
    from product_tracker.db.session import get_engine

    with get_engine().begin() as connection:
        return connection.execute(
            text("SELECT next_allowed_at FROM store_pacing WHERE host = :h"), {"h": HOST}
        ).scalar_one()


def seed_slot(*, seconds_ahead: int = 60) -> datetime:
    """Create the row with its next slot already well into the future.

    That is what makes the advance exact. ``_CLAIM_SLOT`` adds its gap to
    ``greatest(now(), next_allowed_at)``, so a slot in the past would make the first
    claim add "the gap plus however long the test took to get going" -- the same clock
    dependence this test is trying to escape. With the slot ahead of now, every claim
    adds precisely one gap.
    """
    from product_tracker.db.session import get_engine

    with get_engine().begin() as connection:
        connection.execute(
            text("INSERT INTO store_pacing (host) VALUES (:h) ON CONFLICT (host) DO NOTHING"),
            {"h": HOST},
        )
        return connection.execute(
            text(
                "UPDATE store_pacing SET next_allowed_at = now() + make_interval(secs => :s) "
                "WHERE host = :h RETURNING next_allowed_at"
            ),
            {"h": HOST, "s": seconds_ahead},
        ).scalar_one()


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
        """Ten callers at once consume ten different turns, not ten simultaneous ones.

        Measured entirely in the database's clock, on purpose. Two earlier versions of
        this test compared the waits the callers were handed, and both were flaky: a wait
        is a server-side delta, so turning it back into a moment needs a client-side
        reading taken after the round trip, and the round trip is exactly what varies.
        On a slow instance -- a native Windows install rather than the container -- the
        latency spread swallowed a 0.25s gap outright.

        What the guard actually promises is that each caller's claim advances the shared
        row by one gap. Ten claims must therefore advance it by ten. That is the property
        the in-memory throttle broke, and unlike a stopwatch it is exact: if two callers
        raced and both read "free", they would compute from the same base and the row
        would come out short.
        """
        # max_wait raised because the seeded slot is deliberately a minute out; the
        # refusal path has its own tests and is not what this one is about.
        guard = build(interval=0.25, max_wait=200.0)
        before = seed_slot()
        refusals: list[object] = []
        lock = threading.Lock()

        def claim() -> None:
            _wait, refusal = guard._claim(HOST)
            with lock:
                refusals.append(refusal)

        threads = [threading.Thread(target=claim) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert refusals == [None] * 10
        advanced = (read_slot() - before).total_seconds()
        assert advanced == pytest.approx(0.25 * 10, abs=0.01)

    def test_the_later_callers_are_actually_told_to_wait(self) -> None:
        """The coarse half: ten callers cannot all be told to go now. That was the bug --
        every process believed it was alone.

        The interval is five seconds rather than a quarter of one, and that is the whole
        trick. A queue only forms while callers arrive faster than the pacing interval;
        with a short interval on a slow database each claim takes longer than the gap it
        books, ``greatest(now(), next_allowed_at)`` correctly picks ``now()``, and the
        waits come back as zeros. Which is right behaviour, and made an earlier version
        of this test fail for the one reason that is not a bug.
        """
        guard = build(interval=5.0, max_wait=1000.0)
        waits: list[float] = []
        lock = threading.Lock()

        def claim() -> None:
            wait, _refusal = guard._claim(HOST)
            with lock:
                waits.append(wait)

        threads = [threading.Thread(target=claim) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Only the first caller may go immediately; every later one is queued behind it.
        assert sum(1 for w in waits if w > 0) >= 9
        # And the queue really is cumulative -- the last turn is tens of seconds out, not
        # one gap. Nothing is slept: the guard's sleeper is a no-op in these tests.
        assert max(waits) > 20.0


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
