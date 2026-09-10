"""A rate limit that survives more than one API process.

The in-memory limiter was correct and useless at scale: two API processes each kept their
own dict, so a documented ceiling of 60/min was really 60 per replica. The test that
matters is therefore not "does one limiter count" -- it is "do two independent limiter
instances, standing in for two processes, share one ceiling".
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from product_tracker.api.ratelimit import (
    STALE_BUCKET_SECONDS,
    SharedTokenBucketLimiter,
    TokenBucketLimiter,
)

pytestmark = pytest.mark.db


def limiter(rate: int = 60, burst: int = 3) -> SharedTokenBucketLimiter:
    return SharedTokenBucketLimiter(rate_per_minute=rate, burst=burst)


def rewind(client_key: str, seconds: int) -> None:
    """Backdate a bucket's refill clock, in its own committed transaction.

    Deliberately not the ``db_session`` fixture: that holds one outer transaction it rolls
    back at the end, so its row locks are never released to another connection -- and the
    limiter uses its own. Doing it there deadlocked the suite, the fixture waiting on the
    limiter and the limiter waiting on the fixture's lock.
    """
    from product_tracker.db.session import session_scope

    with session_scope() as session:
        session.execute(
            text(
                "UPDATE api_rate_limits SET last_refill = now() - make_interval(secs => :s) "
                "WHERE client_key = :key"
            ),
            {"s": seconds, "key": client_key},
        )


def rows_for(client_key: str) -> int:
    from product_tracker.db.session import session_scope

    with session_scope() as session:
        return int(
            session.execute(
                text("SELECT count(*) FROM api_rate_limits WHERE client_key = :key"),
                {"key": client_key},
            ).scalar_one()
        )


@pytest.fixture(autouse=True)
def _clean(db_env: None) -> None:
    limiter().reset()


class TestOneCeilingAcrossProcesses:
    def test_two_instances_share_the_burst(self) -> None:
        """The whole point. Two objects, one bucket, one ceiling."""
        first, second = limiter(burst=3), limiter(burst=3)

        allowed = [first.allow("1.2.3.4")[0], second.allow("1.2.3.4")[0],
                   first.allow("1.2.3.4")[0]]
        overflow, retry_after = second.allow("1.2.3.4")

        assert allowed == [True, True, True]
        assert overflow is False
        assert retry_after >= 1

    def test_the_in_memory_one_does_not(self) -> None:
        """Stated as a test so the difference is a fact rather than a claim in a docstring.

        This is not a bug in TokenBucketLimiter -- it is the reason the shared one exists.
        """
        first, second = (TokenBucketLimiter(rate_per_minute=60, burst=1) for _ in range(2))

        assert first.allow("1.2.3.4")[0] is True
        assert first.allow("1.2.3.4")[0] is False
        # A second process knows nothing about the first, so its own bucket is full.
        assert second.allow("1.2.3.4")[0] is True

    def test_clients_are_counted_separately(self) -> None:
        shared = limiter(burst=1)

        assert shared.allow("1.1.1.1")[0] is True
        assert shared.allow("1.1.1.1")[0] is False
        assert shared.allow("2.2.2.2")[0] is True

    def test_a_first_request_is_never_refused(self) -> None:
        assert limiter(burst=1).allow("fresh-client")[0] is True


class TestRefill:
    def test_tokens_come_back_over_time(self) -> None:
        shared = limiter(rate=60, burst=1)  # one per second
        assert shared.allow("slow")[0] is True
        assert shared.allow("slow")[0] is False

        # Rewind the clock rather than sleeping: same arithmetic, no wall time.
        rewind("slow", 5)

        assert shared.allow("slow")[0] is True

    def test_refill_is_capped_at_the_burst(self) -> None:
        """An idle client must not accumulate an unbounded allowance and then spend it."""
        shared = limiter(rate=60, burst=2)
        shared.allow("idle")
        rewind("idle", 3600)

        spent = [shared.allow("idle")[0] for _ in range(4)]

        assert spent[:2] == [True, True]
        assert spent[2] is False


class TestDegradesOpen:
    def test_a_database_failure_allows_the_request(self) -> None:
        """A limiter is a guard rail, not an authorisation check.

        Refusing traffic because the limiter cannot reach Postgres would turn a throttle
        into an outage, and readiness already reports a database that is gone.
        """
        from contextlib import contextmanager

        from sqlalchemy.exc import OperationalError

        @contextmanager
        def broken():  # type: ignore[no-untyped-def]
            raise OperationalError("SELECT 1", {}, Exception("no database"))
            yield  # pragma: no cover

        shared = SharedTokenBucketLimiter(
            rate_per_minute=1, burst=1, session_factory=broken
        )

        assert shared.allow("anyone") == (True, 0)


class TestHousekeeping:
    def test_stale_buckets_are_swept(self) -> None:
        """Otherwise a public deployment keeps a row per address it has ever seen."""
        from product_tracker.api import ratelimit

        shared = limiter()
        shared.allow("ancient")
        rewind("ancient", STALE_BUCKET_SECONDS * 2)

        # The sweep is amortised over calls rather than run on a timer, so drive it there.
        shared._calls = ratelimit._SWEEP_EVERY - 1
        shared.allow("current")

        assert rows_for("ancient") == 0
