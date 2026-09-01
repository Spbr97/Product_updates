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

Two implementations. :class:`StoreGuard` keeps its state in memory, which suits a single
worker and is what the tests use. :class:`SharedStoreGuard` keeps it in PostgreSQL, which
is what multi-user search needs: two people searching at once through in-memory guards are
two independent rate limiters, each believing it is the only caller.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.logging import get_logger
from ..db.session import session_scope
from ..domain.models import CheckGuard, GuardDecision

log = get_logger(__name__)


# --- SQL for the shared guard -------------------------------------------------------
#
# Written out once, up here, rather than inline: every one of these runs inside a short
# transaction whose ordering is the point, and having them side by side makes that
# ordering readable.

#: First sighting of a host. Racy by nature, so the conflict is expected, not an error.
_INSERT_HOST = text(
    "INSERT INTO store_pacing (host) VALUES (:host) ON CONFLICT (host) DO NOTHING"
)

#: Serialises the decision. Everything between here and COMMIT is one caller's turn.
_LOCK_HOST = text(
    "SELECT next_allowed_at, open_until, consecutive_failures "
    "FROM store_pacing WHERE host = :host FOR UPDATE"
)

#: The database's clock, not the caller's. Processes on different machines disagree about
#: what time it is, and a shared queue has to be measured by one of them.
_NOW = text("SELECT now()")

_CLEAR_CIRCUIT = text("UPDATE store_pacing SET open_until = NULL WHERE host = :host")

#: Pushes the next slot forward *before* anyone waits, which is what makes the queue work.
#: greatest(now(), next_allowed_at) so a long-idle host starts from now rather than
#: accumulating credit for every second nobody asked about it.
_CLAIM_SLOT = text(
    "UPDATE store_pacing SET "
    "  next_allowed_at = greatest(now(), next_allowed_at) + make_interval(secs => :gap), "
    "  updated_at = now() "
    "WHERE host = :host"
)

_RECORD_SUCCESS = text(
    "UPDATE store_pacing SET consecutive_failures = 0, open_until = NULL, "
    "updated_at = now() WHERE host = :host"
)

_RECORD_FAILURE = text(
    "UPDATE store_pacing SET consecutive_failures = consecutive_failures + 1, "
    "updated_at = now() WHERE host = :host RETURNING consecutive_failures"
)

_OPEN_CIRCUIT = text(
    "UPDATE store_pacing SET open_until = now() + make_interval(secs => :reset) "
    "WHERE host = :host"
)

_SNAPSHOT = text(
    "SELECT host, consecutive_failures, "
    "  greatest(0, extract(epoch from (open_until - now()))) "
    "FROM store_pacing ORDER BY host"
)


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


class SharedStoreGuard:
    """The same pacing and circuit breaking, shared by every process.

    :class:`StoreGuard` keeps its state in memory, which was the right scope while only the
    worker made requests. Search fans out from CLI processes, so two people searching at the
    same moment were two independent rate limiters -- and five were effectively none. This
    keeps the state in PostgreSQL, following ``scheduler/lock.py``: the database is already
    the one thing every process shares.

    Two details carry the whole design, and both are about *when* things happen:

    * **The slot is claimed before the wait, not after it.** ``next_allowed_at`` is pushed
      forward inside the transaction that reads it, so a second process reads an
      already-advanced time and queues behind. Recording the request afterwards would let
      every process read "free" and fire together, which is the bug being fixed.
    * **The row lock is released before sleeping.** Holding it across the wait would
      serialise processes on the lock itself, turning a five-second gap between requests
      into a five-second gap between *decisions* -- and a queue of ten into a minute.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float,
        jitter_seconds: float,
        failure_threshold: int,
        reset_seconds: float,
        max_wait_seconds: float,
        session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.min_interval = min_interval_seconds
        self.jitter_seconds = jitter_seconds
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self.max_wait = max_wait_seconds
        self._sleep = sleeper
        self._jitter = jitter or (lambda high: random.uniform(0, high))
        self._session_factory = session_factory or session_scope

    def before(self, host: str) -> GuardDecision:
        """Claim the next slot for this host, then wait for it."""
        wait, refusal = self._claim(host)
        if refusal is not None:
            return refusal
        if wait > 0:
            self._sleep(wait)
        return GuardDecision.go()

    def _claim(self, host: str) -> tuple[float, GuardDecision | None]:
        """Take the next slot inside one short transaction. Returns (wait, refusal)."""
        with self._session_factory() as session:
            session.execute(_INSERT_HOST, {"host": host})
            next_allowed_at, open_until, failures = session.execute(
                _LOCK_HOST, {"host": host}
            ).one()
            now = session.execute(_NOW).scalar_one()

            if open_until is not None:
                if now < open_until:
                    remaining = int((open_until - now).total_seconds())
                    return 0.0, GuardDecision.skip(
                        f"{host} has failed {failures} times in a row; backing off for "
                        f"another {remaining}s before trying again"
                    )
                # Cooling-off elapsed: half-open. Let one probe through, for one process.
                session.execute(_CLEAR_CIRCUIT, {"host": host})
                log.info("store.circuit_half_open", host=host, shared=True)

            wait = max(0.0, (next_allowed_at - now).total_seconds())
            if wait > self.max_wait:
                # Ten people searching at once should not each wait a minute for the tenth
                # slot. Refusing is honest, and lets the caller report a throttled store.
                return 0.0, GuardDecision.skip(
                    f"{host} is busy: the next free slot is {int(wait)}s away, beyond the "
                    f"{int(self.max_wait)}s a single request will wait"
                )

            gap = self.min_interval + self._jitter(self.jitter_seconds)
            session.execute(_CLAIM_SLOT, {"host": host, "gap": gap})
        return wait, None

    def after(self, host: str, *, succeeded: bool) -> None:
        """Record the outcome, opening the circuit for every process at once."""
        with self._session_factory() as session:
            # The host may have no row yet: a failure can be reported for a request that
            # was never paced. Without this the UPDATE matches nothing, the failure is
            # silently dropped, and the circuit never opens.
            session.execute(_INSERT_HOST, {"host": host})

            if succeeded:
                session.execute(_RECORD_SUCCESS, {"host": host})
                return

            failures = session.execute(_RECORD_FAILURE, {"host": host}).scalar()
            if failures is not None and failures >= self.failure_threshold:
                session.execute(
                    _OPEN_CIRCUIT, {"host": host, "reset": self.reset_seconds}
                )
                log.warning(
                    "store.circuit_open",
                    host=host,
                    consecutive_failures=failures,
                    reset_seconds=self.reset_seconds,
                    shared=True,
                )

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Current state per host, for ``product-tracker status``."""
        with self._session_factory() as session:
            rows = session.execute(_SNAPSHOT).all()
        return {
            host: {
                "consecutive_failures": failures,
                "circuit_open": bool(opens_for and opens_for > 0),
                "opens_for_seconds": int(opens_for or 0),
            }
            for host, failures, opens_for in rows
        }


def build_guard(settings: Settings) -> CheckGuard:
    """The guard this deployment should use.

    Shared by default. The in-memory one remains for tests and for anywhere without a
    database, but it is only correct when a single process makes all the requests -- which
    stopped being true the moment search could be run by several people at once.
    """
    if settings.shared_pacing:
        return SharedStoreGuard(
            min_interval_seconds=settings.store_min_interval_seconds,
            jitter_seconds=settings.fetch_jitter_seconds,
            failure_threshold=settings.store_failure_threshold,
            reset_seconds=settings.store_circuit_reset_seconds,
            max_wait_seconds=settings.store_max_wait_seconds,
        )
    return StoreGuard(
        min_interval_seconds=settings.store_min_interval_seconds,
        jitter_seconds=settings.fetch_jitter_seconds,
        failure_threshold=settings.store_failure_threshold,
        reset_seconds=settings.store_circuit_reset_seconds,
    )
