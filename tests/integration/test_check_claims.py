"""Claiming a check, so several workers are safe.

The guarantee that matters is not "a claim can be taken" -- it is "two workers racing for
the same product produce exactly one winner". Everything else here exists because a claim
lives in a row rather than in a connection, so unlike an advisory lock it survives the
death of the thing holding it, and that has to be handled rather than hoped about.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from product_tracker.db.models import Product
from product_tracker.db.session import session_scope
from product_tracker.scheduler import claims

pytestmark = pytest.mark.db


@pytest.fixture
def product_id(db_env: None) -> int:
    """A committed product, since the claim runs in its own connection."""
    with session_scope() as session:
        store = session.execute(text("SELECT id FROM stores LIMIT 1")).scalar_one()
        url = "https://shop.example.com/p/claimed"
        session.execute(text("DELETE FROM products WHERE url_canonical = :u"), {"u": url})
        product = Product(url=url, url_canonical=url, store_id=store, name="Claimed")
        session.add(product)
        session.flush()
        return int(product.id)


def held_by(product_id: int) -> str | None:
    with session_scope() as session:
        return session.execute(
            text("SELECT check_claimed_by FROM products WHERE id = :i"), {"i": product_id}
        ).scalar_one()


def age_claim(product_id: int, seconds: int) -> None:
    with session_scope() as session:
        session.execute(
            text(
                "UPDATE products SET check_claimed_at = now() - make_interval(secs => :s) "
                "WHERE id = :i"
            ),
            {"s": seconds, "i": product_id},
        )


class TestExactlyOneWinner:
    def test_a_second_worker_is_refused(self, product_id: int) -> None:
        assert claims.claim(product_id, worker="worker-a") is True
        assert claims.claim(product_id, worker="worker-b") is False

    def test_racing_workers_produce_one_winner(self, product_id: int) -> None:
        """The actual race, run concurrently rather than argued about.

        Sequential calls would pass even against a read-then-write implementation, which
        is precisely the bug being closed -- so this fires them at once.
        """
        with ThreadPoolExecutor(max_workers=8) as pool:
            granted = list(
                pool.map(lambda n: claims.claim(product_id, worker=f"w{n}"), range(8))
            )

        assert sum(granted) == 1, f"{sum(granted)} workers claimed the same check"

    def test_releasing_lets_the_next_worker_in(self, product_id: int) -> None:
        claims.claim(product_id, worker="worker-a")
        claims.release(product_id, worker="worker-a")

        assert claims.claim(product_id, worker="worker-b") is True

    def test_a_different_product_is_unaffected(self, product_id: int) -> None:
        """Claims are per check, not a global lock -- otherwise several workers would be
        no better than one."""
        with session_scope() as session:
            store = session.execute(text("SELECT id FROM stores LIMIT 1")).scalar_one()
            other = Product(
                url="https://shop.example.com/p/other",
                url_canonical="https://shop.example.com/p/other",
                store_id=store,
                name="Other",
            )
            session.add(other)
            session.flush()
            other_id = int(other.id)

        assert claims.claim(product_id, worker="worker-a") is True
        assert claims.claim(other_id, worker="worker-a") is True


class TestTheClaimIsALease:
    def test_an_expired_claim_can_be_taken_over(self, product_id: int) -> None:
        """A worker killed mid-check leaves the row set with nothing running behind it.

        An advisory lock dies with its connection; a row does not. Without expiry, one
        `kill -9` would stall that product for ever.
        """
        claims.claim(product_id, worker="dead-worker", lease_seconds=60)
        age_claim(product_id, 120)

        assert claims.claim(product_id, worker="live-worker", lease_seconds=60) is True
        assert held_by(product_id) == "live-worker"

    def test_a_fresh_claim_is_not_stolen(self, product_id: int) -> None:
        claims.claim(product_id, worker="busy-worker", lease_seconds=900)
        age_claim(product_id, 60)

        assert claims.claim(product_id, worker="impatient", lease_seconds=900) is False

    def test_release_only_touches_your_own_claim(self, product_id: int) -> None:
        """After a takeover the previous holder must not be able to release the new one:
        it would hand a third worker a check that is actively running."""
        claims.claim(product_id, worker="first", lease_seconds=60)
        age_claim(product_id, 120)
        claims.claim(product_id, worker="second", lease_seconds=60)

        claims.release(product_id, worker="first")

        assert held_by(product_id) == "second"


class TestContextManager:
    def test_it_releases_on_the_way_out(self, product_id: int) -> None:
        with claims.claimed(product_id, worker="w") as granted:
            assert granted is True
        assert held_by(product_id) is None

    def test_it_releases_even_when_the_body_raises(self, product_id: int) -> None:
        """A check that blew up has still finished. Leaving the lease to expire would
        stall that product for fifteen minutes over an exception we already handled."""
        with pytest.raises(RuntimeError), claims.claimed(product_id, worker="w"):
            raise RuntimeError("the check exploded")

        assert held_by(product_id) is None

    def test_it_does_not_release_a_claim_it_never_held(self, product_id: int) -> None:
        claims.claim(product_id, worker="holder")

        with claims.claimed(product_id, worker="loser") as granted:
            assert granted is False

        assert held_by(product_id) == "holder"


class TestDegradesClosed:
    def test_an_unreachable_database_declines(self) -> None:
        """The opposite choice from the rate limiter, deliberately.

        A limiter that cannot reach the database should let traffic through: it is a guard
        rail. A claim that cannot reach the database must decline, because proceeding
        unclaimed is exactly the duplicate-check bug it exists to prevent, and the next
        scheduled pass will retry in minutes.
        """
        from contextlib import contextmanager

        from sqlalchemy.exc import OperationalError

        @contextmanager
        def broken():  # type: ignore[no-untyped-def]
            raise OperationalError("SELECT 1", {}, Exception("no database"))
            yield  # pragma: no cover

        assert claims.claim(1, worker="w", session_factory=broken) is False


class TestIdentity:
    def test_it_names_a_host_and_pid(self) -> None:
        """"Which worker is stuck on product 42" is the first question anyone asks, and a
        UUID cannot answer it."""
        import os

        identity = claims.worker_identity()

        assert str(os.getpid()) in identity
        assert len(identity) <= 64
