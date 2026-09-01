"""Per-client rate limiting.

The store-facing throttle protects retailers from us. This protects *this* API from its
callers -- a script looping on ``/check`` would otherwise drive unbounded outbound requests
to real shops, which is the failure mode that gets an IP blocked.

A token bucket per client: a steady refill rate with a burst allowance, so ordinary
interactive use never notices while a runaway loop is held to the configured rate.

**In-memory and per-process.** Two API processes each enforce their own limit, so the
effective ceiling is the limit times the process count. That is the honest scope for a
single-user tool; a shared limit needs shared state, which means Redis, which is not worth
introducing for this.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

#: 429. Spelled numerically: Starlette renames its constants between versions.
_HTTP_429 = 429

#: Methods that change state or cause outbound requests. Reads are cheap and local.
LIMITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Paths a probe uses. Rate-limiting a health check would take the service out of a load
#: balancer under exactly the load the limit exists to survive.
EXEMPT_PATHS = frozenset({"/health", "/health/ready"})


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

    def __init__(self, app: ASGIApp, limiter: TokenBucketLimiter) -> None:
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
