"""Making "one worker per database" enforced rather than merely documented.

APScheduler's job store has no cross-process locking, so two workers against one database
each run every job: every product checked twice, every store hit twice as hard, and the
only symptom is duplicated rows that look like a bug elsewhere. Documenting the constraint
does not stop someone starting a second worker by accident -- a stray terminal, a systemd
unit plus a container, a Compose scale-up.

A PostgreSQL *session-level advisory lock* turns that silent doubling into a loud refusal.
It is the right tool here because the database is already the one thing every worker shares,
and because the lock dies with its connection: a worker that is killed, panics, or loses
the network releases it without any cleanup or lease-expiry logic to get wrong.

This does not make multiple workers *work* -- it makes the second one decline to start.
Running several is a Celery-shaped problem, and ``JobQueue`` is where that would go.
"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy import Connection, text

from ..core.logging import get_logger
from ..db.session import get_engine

log = get_logger(__name__)

#: Arbitrary but fixed. Advisory locks share one namespace per database, so the value only
#: has to be stable and unlikely to collide with another application's.
WORKER_LOCK_KEY = 0x50545F57524B  # "PT_WRK"


class WorkerAlreadyRunningError(RuntimeError):
    """Another worker holds the lock on this database."""

    def __init__(self, holder: str | None = None) -> None:
        detail = f" (held by {holder})" if holder else ""
        super().__init__(
            f"another worker is already running against this database{detail}. "
            "Only one worker may run per database: two would each execute every job, "
            "checking every product twice."
        )
        self.holder = holder


class WorkerLock:
    """Session-level advisory lock, held for as long as this worker runs.

    Used as a context manager. Holds its own connection, deliberately outside the pool's
    normal churn: the lock lives exactly as long as that connection does.
    """

    def __init__(self, key: int = WORKER_LOCK_KEY) -> None:
        self.key = key
        self._connection: Connection | None = None

    def acquire(self) -> bool:
        """Try to take the lock. Returns False if another worker holds it."""
        connection = get_engine().connect()
        try:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": self.key}
                ).scalar()
            )
        except Exception:
            connection.close()
            raise

        if not acquired:
            connection.close()
            return False

        self._connection = connection
        log.info("worker.lock_acquired", key=self.key)
        return True

    def release(self) -> None:
        """Drop the lock. Safe to call when it was never held."""
        if self._connection is None:
            return
        try:
            self._connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": self.key}
            )
        except Exception as exc:
            # The connection may already be gone, which released the lock anyway.
            log.debug("worker.unlock_failed", exc_info=exc)
        finally:
            self._connection.close()
            self._connection = None
            log.info("worker.lock_released", key=self.key)

    def holder(self) -> str | None:
        """Describe whoever holds the lock, for the error message.

        Best effort: ``pg_stat_activity`` may be restricted, and the holder may have gone
        between the failed acquire and this query.
        """
        try:
            with get_engine().connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT a.application_name, a.client_addr, a.pid "
                        "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE l.locktype = 'advisory' AND l.objid = :objid "
                        "AND l.granted LIMIT 1"
                    ),
                    # Advisory keys are split across classid/objid; objid is the low word.
                    {"objid": self.key & 0xFFFFFFFF},
                ).first()
        except Exception:
            return None
        if row is None:
            return None
        name, address, pid = row
        parts = [str(part) for part in (name, address, f"pid {pid}") if part]
        return ", ".join(parts) or None

    def __enter__(self) -> WorkerLock:
        if not self.acquire():
            raise WorkerAlreadyRunningError(self.holder())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
