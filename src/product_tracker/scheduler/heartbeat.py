"""Worker liveness, measured rather than inferred.

The worker writes a row on startup and touches it on every reconcile. Any other process --
the API, the CLI -- reads it from PostgreSQL and gets a direct answer to "is a worker
running?", instead of guessing from whether scheduled jobs look overdue.

A heartbeat is considered stale after a few reconcile intervals. That tolerance matters:
one missed beat is a slow check, not a dead worker.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.models import WorkerHeartbeat

log = get_logger(__name__)

#: How many reconcile intervals may pass before a worker is presumed gone.
STALE_INTERVALS = 3
#: Floor, so a very short reconcile interval does not make the check hair-trigger.
MIN_STALE_SECONDS = 120


def new_worker_id() -> str:
    """A fresh id for this process. Not the hostname or pid: both get reused."""
    return uuid.uuid4().hex


def stale_after(reconcile_interval_seconds: int) -> int:
    return max(MIN_STALE_SECONDS, reconcile_interval_seconds * STALE_INTERVALS)


def touch(session: Session, worker_id: str) -> None:
    """Record that this worker is alive, now.

    Upsert rather than update: the first beat creates the row and every later one moves
    ``last_seen_at``, with no read-then-write in between.
    """
    now = datetime.now(UTC)
    stmt = (
        insert(WorkerHeartbeat)
        .values(
            worker_id=worker_id,
            hostname=socket.gethostname()[:255],
            pid=os.getpid(),
            started_at=now,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=["worker_id"], set_={"last_seen_at": now}
        )
    )
    session.execute(stmt)


def clear(session: Session, worker_id: str) -> None:
    """Remove this worker's heartbeat on a clean shutdown.

    So a deliberately stopped worker reads as "not running" immediately, rather than
    "running" until its heartbeat goes stale.
    """
    row = session.execute(
        select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id)
    ).scalar_one_or_none()
    if row is not None:
        session.delete(row)


@dataclass(frozen=True, slots=True)
class WorkerLiveness:
    """What the heartbeat table says."""

    #: False when the table does not exist yet (migrations not applied).
    available: bool
    workers: int = 0
    last_seen_at: datetime | None = None
    seconds_since: int | None = None
    stale_after_seconds: int = MIN_STALE_SECONDS

    @property
    def running(self) -> bool | None:
        """True, False, or None when nothing has ever reported in."""
        if not self.available or self.last_seen_at is None:
            return None
        return (self.seconds_since or 0) <= self.stale_after_seconds

    @property
    def detail(self) -> str:
        if not self.available:
            return "heartbeat table not found; run 'alembic upgrade head'"
        if self.last_seen_at is None:
            return "no worker has ever reported in"
        if self.running:
            extra = f"; {self.workers} workers reporting" if self.workers > 1 else ""
            return f"last beat {self.seconds_since}s ago{extra}"
        return f"last beat {self.seconds_since}s ago, over the {self.stale_after_seconds}s limit"


def read(session: Session, *, reconcile_interval_seconds: int) -> WorkerLiveness:
    """Read worker liveness. Never raises."""
    limit = stale_after(reconcile_interval_seconds)
    try:
        rows = session.execute(select(WorkerHeartbeat.last_seen_at)).scalars().all()
    except Exception:
        return WorkerLiveness(available=False, stale_after_seconds=limit)

    if not rows:
        return WorkerLiveness(available=True, stale_after_seconds=limit)

    latest = max(rows)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    since = int((datetime.now(UTC) - latest).total_seconds())

    return WorkerLiveness(
        available=True,
        workers=len(rows),
        last_seen_at=latest,
        seconds_since=max(0, since),
        stale_after_seconds=limit,
    )


def prune(session: Session, *, older_than_seconds: int) -> int:
    """Drop heartbeats from workers that stopped without cleaning up (a crash, a kill -9)."""
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    rows = (
        session.execute(select(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at < cutoff))
        .scalars()
        .all()
    )
    for row in rows:
        session.delete(row)
    return len(rows)
