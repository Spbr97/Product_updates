"""Being a good citizen towards the sites we fetch.

Two independent protections, both keyed by **host**.

Host, not adapter slug: several unrelated retailers share the generic adapter, so keying
on the adapter would put them in one throttle bucket and let one blocked site open a
circuit that skips every other site using that adapter. Politeness is owed to a server.

* **Throttle** -- a minimum gap between requests to the same host, plus randomised
  jitter. Without jitter, a worker checking twenty Flipkart products would hit the site in
  a perfectly regular pattern, which is both rude and conspicuous.
* **Circuit breaker** -- after a run of consecutive failures for one host, stop calling it
  for a cooling-off period. This is what stops a blocked or down site from being hammered,
  and it is per-host, so one failing site never delays products from any other.

State is in-memory and per-process, which is the right scope: it exists to pace *this*
worker's outgoing requests. A one-shot CLI check has no pacing to do, so it uses no guard.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..core.logging import get_logger
from ..domain.models import GuardDecision

log = get_logger(__name__)


@dataclass
class _StoreState:
    last_request_at: float | None = None
    consecutive_failures: int = 0
    open_until: float | None = None


class StoreGuard:
    """Per-store throttling and circuit breaking.

    Thread-safe: APScheduler runs jobs in a thread pool, so several products from the same
    store can be checked concurrently and must share this state.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float,
        jitter_seconds: float,
        failure_threshold: int,
        reset_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.min_interval = min_interval_seconds
        self.jitter_seconds = jitter_seconds
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._clock = clock
        self._sleep = sleeper
        # Injectable so tests are deterministic rather than merely usually-right.
        self._jitter = jitter or (lambda high: random.uniform(0, high))
        self._states: dict[str, _StoreState] = {}
        self._lock = threading.Lock()

    def before(self, host: str) -> GuardDecision:
        """Decide whether to check, waiting out the throttle if the answer is yes."""
        with self._lock:
            state = self._states.setdefault(host, _StoreState())
            now = self._clock()

            if state.open_until is not None:
                if now < state.open_until:
                    remaining = int(state.open_until - now)
                    return GuardDecision.skip(
                        f"{host} has failed {state.consecutive_failures} times in a row; "
                        f"backing off for another {remaining}s before trying again"
                    )
                # Cooling-off elapsed: half-open. Allow one probe through.
                state.open_until = None
                log.info("store.circuit_half_open", host=host)

            wait = 0.0
            if state.last_request_at is not None:
                elapsed = now - state.last_request_at
                if elapsed < self.min_interval:
                    wait = self.min_interval - elapsed
            wait += self._jitter(self.jitter_seconds)
            # Claim the slot before releasing the lock, so two threads cannot both
            # decide the store is free and fire simultaneously.
            state.last_request_at = now + wait

        if wait > 0:
            self._sleep(wait)
        return GuardDecision.go()

    def after(self, host: str, *, succeeded: bool) -> None:
        """Record the outcome, opening the circuit once failures pile up."""
        with self._lock:
            state = self._states.setdefault(host, _StoreState())
            if succeeded:
                if state.consecutive_failures:
                    log.info("store.recovered", host=host)
                state.consecutive_failures = 0
                state.open_until = None
                return

            state.consecutive_failures += 1
            if state.consecutive_failures >= self.failure_threshold:
                state.open_until = self._clock() + self.reset_seconds
                log.warning(
                    "store.circuit_open",
                    host=host,
                    consecutive_failures=state.consecutive_failures,
                    reset_seconds=self.reset_seconds,
                )

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Current state per host, for ``product-tracker status``."""
        with self._lock:
            now = self._clock()
            return {
                slug: {
                    "consecutive_failures": state.consecutive_failures,
                    "circuit_open": state.open_until is not None and now < state.open_until,
                    "opens_for_seconds": (
                        max(0, int(state.open_until - now)) if state.open_until else 0
                    ),
                }
                for slug, state in self._states.items()
            }

