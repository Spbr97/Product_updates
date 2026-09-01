"""The API's own rate limit.

Driven by an injected clock, so refill behaviour is asserted exactly rather than waited for.
"""

from __future__ import annotations

import pytest

from product_tracker.api.ratelimit import (
    EXEMPT_PATHS,
    LIMITED_METHODS,
    TokenBucketLimiter,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def limiter(clock: FakeClock, *, rate: int = 60, burst: int = 5) -> TokenBucketLimiter:
    return TokenBucketLimiter(rate_per_minute=rate, burst=burst, clock=clock)


class TestBurst:
    def test_a_new_client_starts_full(self, clock: FakeClock) -> None:
        """A first request must never be rejected."""
        bucket = limiter(clock, burst=5)

        assert all(bucket.allow("client")[0] for _ in range(5))

    def test_the_burst_is_exhausted(self, clock: FakeClock) -> None:
        bucket = limiter(clock, burst=3)
        for _ in range(3):
            bucket.allow("client")

        allowed, retry_after = bucket.allow("client")

        assert not allowed
        assert retry_after >= 1


class TestRefill:
    def test_tokens_come_back_over_time(self, clock: FakeClock) -> None:
        bucket = limiter(clock, rate=60, burst=2)  # one per second
        bucket.allow("client")
        bucket.allow("client")
        assert not bucket.allow("client")[0]

        clock.advance(1.0)

        assert bucket.allow("client")[0]

    def test_refill_is_capped_at_the_burst(self, clock: FakeClock) -> None:
        """An idle client must not bank unlimited credit."""
        bucket = limiter(clock, rate=60, burst=3)
        clock.advance(3600)

        assert sum(bucket.allow("client")[0] for _ in range(10)) == 3

    def test_retry_after_is_actionable(self, clock: FakeClock) -> None:
        bucket = limiter(clock, rate=60, burst=1)
        bucket.allow("client")

        _, retry_after = bucket.allow("client")

        clock.advance(retry_after)
        assert bucket.allow("client")[0]


class TestIsolation:
    def test_clients_have_separate_buckets(self, clock: FakeClock) -> None:
        """One noisy caller must not lock everyone else out."""
        bucket = limiter(clock, burst=2)
        bucket.allow("noisy")
        bucket.allow("noisy")
        assert not bucket.allow("noisy")[0]

        assert bucket.allow("quiet")[0]

    def test_reset_clears_everything(self, clock: FakeClock) -> None:
        bucket = limiter(clock, burst=1)
        bucket.allow("client")

        bucket.reset()

        assert bucket.allow("client")[0]


class TestScope:
    def test_only_state_changing_methods_are_limited(self) -> None:
        assert "GET" not in LIMITED_METHODS
        assert {"POST", "DELETE"} <= LIMITED_METHODS

    def test_probes_are_exempt(self) -> None:
        """Limiting a health check would pull the service from a load balancer under
        exactly the load the limit exists to survive."""
        assert "/health" in EXEMPT_PATHS
        assert "/health/ready" in EXEMPT_PATHS
