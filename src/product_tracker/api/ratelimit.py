"""Per-client rate limiting.

The store-facing throttle protects retailers from us. This protects *this* API from its
callers -- a script looping on ``/check`` would otherwise drive unbounded outbound requests
to real shops, which is the failure mode that gets an IP blocked.

A token bucket per client: a steady refill rate with a burst allowance, so ordinary
interactive use never notices while a runaway loop is held to the configured rate.

Two implementations, both satisfying :class:`Limiter`. :class:`TokenBucketLimiter` keeps
its buckets in a dict -- correct and free when one process serves everything.
:class:`SharedTokenBucketLimiter` keeps them in PostgreSQL, so the configured ceiling is
the ceiling however many API processes are running.

The shared one is the default, for the same reason ``store_pacing`` is: a limit that
silently multiplies by the replica count is not a limit, and this project already treats
the database as the thing every process shares rather than introducing Redis to coordinate
what Postgres can. The cost is one indexed statement per *mutating* request -- reads and
health probes never touch it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..core.logging import get_logger
from ..db.session import session_scope

log = get_logger(__name__)

#: 429. Spelled numerically: Starlette renames its constants between versions.
_HTTP_429 = 429

#: Methods that change state or cause outbound requests. Reads are cheap and local.
LIMITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Paths a probe uses. Rate-limiting a health check would take the service out of a load
#: balancer under exactly the load the limit exists to survive.
EXEMPT_PATHS = frozenset({"/health", "/health/ready"})


class Limiter(Protocol):
    """What the middleware needs. Both implementations satisfy it, and the middleware is
    written against this so swapping the backing store is a one-line change in the app
    factory rather than an edit to the request path."""

    rate_per_minute: int

    def allow(self, key: str) -> tuple[bool, int]:
        """Take a token. Returns ``(allowed, retry_after_seconds)``."""
        ...

    def reset(self) -> None: ...


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


@dataclass
class TokenBucketLimiter:
    """Refills at ``rate_per_minute``, holds at most ``burst`` tokens."""

    rate_per_minute: int
    burst: int
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> tuple[bool, int]:
        """Take a token. Returns ``(allowed, retry_after_seconds)``."""
        per_second = self.rate_per_minute / 60.0
        now = self.clock()

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # A new client starts full, so a first request is never rejected.
                bucket = _Bucket(tokens=float(self.burst), last_refill=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.last_refill)
                bucket.tokens = min(self.burst, bucket.tokens + elapsed * per_second)
                bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0

            # How long until one token is available again.
            missing = 1.0 - bucket.tokens
            return False, max(1, int(missing / per_second) + 1)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


#: How long an untouched bucket is kept before the sweep removes it. Long enough that a
#: returning client is still rate-limited rather than handed a fresh burst, short enough
#: that a public deployment does not keep a row per address it ever saw.
STALE_BUCKET_SECONDS = 3600

#: One sweep per this many calls, rather than a timer: the cost then scales with traffic,
#: and an idle deployment does no work at all.
_SWEEP_EVERY = 500


@dataclass
class SharedTokenBucketLimiter:
    """The same bucket, in PostgreSQL, so every API process shares one ceiling.

    The whole decision is in a single statement. Refill, spend and store happen inside one
    ``UPDATE``, so two processes cannot both read "one token left" and both spend it --
    which is precisely the race that made the in-memory version wrong across replicas. A
    row that comes back means the token was taken; no row means it was not there.

    Falls back to allowing the request if the database is unreachable. A rate limiter is a
    guard rail, not an authorisation check: taking the API down because the limiter cannot
    reach Postgres would turn a throttle into an outage, and the readiness probe already
    reports a database that is gone.
    """

    rate_per_minute: int
    burst: int
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None
    _calls: int = 0

    def _sessions(self) -> Callable[[], AbstractContextManager[Session]]:
        return self.session_factory or session_scope

    def allow(self, key: str) -> tuple[bool, int]:
        per_second = self.rate_per_minute / 60.0
        params = {"key": key[:128], "burst": float(self.burst), "rate": per_second}

        try:
            with self._sessions()() as session:
                # A first-time caller starts full, so their first request is never refused.
                session.execute(
                    text(
                        "INSERT INTO api_rate_limits (client_key, tokens, last_refill) "
                        "VALUES (:key, :burst, now()) ON CONFLICT (client_key) DO NOTHING"
                    ),
                    params,
                )
                spent = session.execute(text(_SPEND), params).first()
                if spent is None:
                    remaining = session.execute(text(_REMAINING), params).scalar() or 0.0
                    self._maybe_sweep(session)
                    missing = max(0.0, 1.0 - float(remaining))
                    return False, max(1, int(missing / per_second) + 1)
                self._maybe_sweep(session)
                return True, 0
        except SQLAlchemyError as exc:
            # Guard rail, not gate: see the class docstring.
            log.warning("ratelimit.unavailable", error=type(exc).__name__)
            return True, 0

    def _maybe_sweep(self, session: Session) -> None:
        self._calls += 1
        if self._calls % _SWEEP_EVERY:
            return
        session.execute(
            text(
                "DELETE FROM api_rate_limits "
                "WHERE last_refill < now() - make_interval(secs => :age)"
            ),
            {"age": STALE_BUCKET_SECONDS},
        )

    def reset(self) -> None:
        """Drop every bucket. For tests, and for an operator clearing a mistake."""
        with self._sessions()() as session:
            session.execute(text("DELETE FROM api_rate_limits"))


#: Refill by elapsed time, cap at the burst, spend one -- atomically. The WHERE clause is
#: the guard: it repeats the refill expression so the row is only updated when a whole
#: token is actually available, and Postgres evaluates it against the row it has locked.
_REFILL = (
    "LEAST(:burst, tokens + EXTRACT(EPOCH FROM (now() - last_refill)) * :rate)"
)
_SPEND = (
    f"UPDATE api_rate_limits SET tokens = {_REFILL} - 1, last_refill = now() "
    f"WHERE client_key = :key AND {_REFILL} >= 1 RETURNING tokens"
)
_REMAINING = (
    f"SELECT {_REFILL} FROM api_rate_limits WHERE client_key = :key"
)


def client_key(request: Request) -> str:
    """Identify the caller.

    The peer address, not ``X-Forwarded-For``: that header is client-supplied and trivially
    spoofed, so trusting it would let anyone bypass the limit by varying it. Behind a proxy
    the limit therefore applies to the proxy -- correct for a localhost tool, and something
    to revisit alongside a real deployment story.
    """
    client = request.client
    return client.host if client else "unknown"


class RateLimitMiddleware:
    """Reject callers exceeding the configured rate on state-changing requests."""

    def __init__(self, app: ASGIApp, limiter: Limiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        if request.method not in LIMITED_METHODS or request.url.path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        allowed, retry_after = self.limiter.allow(client_key(request))
        if allowed:
            await self.app(scope, receive, send)
            return

        message = (
            f"too many requests; retry in {retry_after}s "
            f"(limit {self.limiter.rate_per_minute}/min)"
        )
        response = JSONResponse(
            status_code=_HTTP_429,
            content={"error": {"type": "rate_limited", "message": message}},
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)
