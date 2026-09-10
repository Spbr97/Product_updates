"""Claiming a check, so two workers never perform the same one.

``lock.py`` makes a *second worker* refuse to start. This makes a *single check* exclusive
instead, which is the smaller and more useful guarantee: with it, running several workers
is safe, and the one that dies mid-check does not take the schedule with it.

Three properties carry the design.

**The claim is taken in the same statement that tests it.** A read-then-write would let two
workers both see "free" and both proceed, which is the exact race being closed. The
``UPDATE … WHERE … RETURNING`` either returns a row -- the claim is yours -- or returns
nothing, and no row means somebody else has it. There is no third answer to get wrong.

**It is a lease, not a lock.** A worker that is killed, panics, or loses the network leaves
``check_claimed_at`` set with nothing running behind it. An advisory lock would be released
by the dying connection; a row will not be. So a claim older than the lease is claimable
again, and the lease is set well above the longest a check can honestly take.

**Releasing is best-effort.** If the release fails, the lease expiry is the backstop; the
check has already happened and been recorded either way. Nothing here may raise into the
check path, because a bookkeeping failure must not turn a successful check into a failed
one.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.session import session_scope

log = get_logger(__name__)

#: How long a claim stands before another worker may take it. Generously above the worst
#: honest case -- an HTTP timeout times the retry budget, plus a browser render -- because
#: reclaiming a check that is merely slow means running it twice, which is the thing this
#: module exists to prevent.
DEFAULT_LEASE_SECONDS = 900

_CLAIM = """
UPDATE products
   SET check_claimed_at = now(), check_claimed_by = :worker
 WHERE id = :product_id
   AND (
        check_claimed_at IS NULL
     OR check_claimed_at < now() - make_interval(secs => :lease)
   )
RETURNING id
"""

#: Scoped to this worker: releasing a claim someone else has taken over after our lease
#: expired would hand them a check we are no longer entitled to interrupt.
_RELEASE = """
UPDATE products
   SET check_claimed_at = NULL, check_claimed_by = NULL
 WHERE id = :product_id AND check_claimed_by = :worker
"""


def worker_identity() -> str:
    """Something a human can act on when a claim is stuck. Host and pid, not a UUID."""
    return f"{socket.gethostname()}:{os.getpid()}"[:64]


def claim(
    product_id: int,
    *,
    worker: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
) -> bool:
    """Take the right to check this product. False when another worker holds it."""
    sessions = session_factory or session_scope
    params = {"product_id": product_id, "worker": worker, "lease": float(lease_seconds)}
    try:
        with sessions() as session:
            return session.execute(text(_CLAIM), params).first() is not None
    except SQLAlchemyError as exc:
        # Unreachable database: decline rather than proceed. Running unclaimed is how the
        # duplicate-check bug comes back, and the next scheduled pass will retry.
        log.warning("claim.unavailable", product_id=product_id, error=type(exc).__name__)
        return False


def release(
    product_id: int,
    *,
    worker: str,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
) -> None:
    """Give the claim back. Failure is survivable -- the lease expires anyway."""
    sessions = session_factory or session_scope
    try:
        with sessions() as session:
            session.execute(
                text(_RELEASE), {"product_id": product_id, "worker": worker}
            )
    except SQLAlchemyError as exc:
        log.warning("claim.release_failed", product_id=product_id, error=type(exc).__name__)


@contextmanager
def claimed(
    product_id: int,
    *,
    worker: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
) -> Iterator[bool]:
    """Hold a claim for the duration of the block.

    Yields whether it was granted, rather than raising or skipping silently, so the caller
    decides what "somebody else has it" means. The claim is released even when the body
    raises: a check that blew up has still finished, and leaving the lease to expire would
    stall that product for fifteen minutes.
    """
    who = worker or worker_identity()
    granted = claim(
        product_id, worker=who, lease_seconds=lease_seconds, session_factory=session_factory
    )
    try:
        yield granted
    finally:
        if granted:
            release(product_id, worker=who, session_factory=session_factory)
