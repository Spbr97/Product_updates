"""The job-queue interface.

The worker talks to this, never to APScheduler directly. Swapping in Celery, RQ, or a
cloud scheduler later means writing one more implementation -- the tracking engine, the
services, and the reconcile logic are untouched.

The contract that matters is :meth:`JobQueue.reconcile`: given the products that *should*
be scheduled, make the queue match. Everything else follows from that, including recovery
after a restart and picking up products added through the API while the worker runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What one reconcile pass changed."""

    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def __str__(self) -> str:
        return (
            f"added={self.added} updated={self.updated} "
            f"removed={self.removed} unchanged={self.unchanged}"
        )


def job_id_for(product_id: int) -> str:
    """The deterministic job id for a product.

    Deterministic on purpose: scheduling the same product twice replaces the existing job
    rather than creating a second one, so a restart, a reconcile, and a manual reschedule
    can never leave a product being checked twice.
    """
    return f"product:{product_id}"


def product_id_from(job_id: str) -> int | None:
    """Inverse of :func:`job_id_for`. ``None`` for jobs we did not create."""
    prefix = "product:"
    if not job_id.startswith(prefix):
        return None
    try:
        return int(job_id[len(prefix) :])
    except ValueError:
        return None


class JobQueue(ABC):
    """Schedules recurring per-product checks."""

    @abstractmethod
    def start(self, *, paused: bool = False) -> None:
        """Begin accepting jobs.

        ``paused=True`` brings the queue up without executing anything, which is how the
        worker gets a consistent schedule installed before any check fires.
        """

    @abstractmethod
    def resume(self) -> None:
        """Begin executing jobs after a paused start."""

    @abstractmethod
    def shutdown(self, *, wait: bool = True) -> None:
        """Stop executing jobs, optionally waiting for running ones to finish."""

    @abstractmethod
    def schedule_product(self, product_id: int, interval_seconds: int) -> None:
        """Schedule (or reschedule) one product. Idempotent."""

    @abstractmethod
    def unschedule_product(self, product_id: int) -> None:
        """Remove a product's job. Does nothing if it is not scheduled."""

    @abstractmethod
    def scheduled_products(self) -> dict[int, int]:
        """Currently scheduled ``{product_id: interval_seconds}``."""

    @abstractmethod
    def schedule_recurring(
        self, job_id: str, func: object, interval_seconds: int, *, first_run_now: bool = False
    ) -> None:
        """Schedule a housekeeping job that is not tied to a product."""

    def reconcile(self, desired: dict[int, int]) -> ReconcileReport:
        """Make the queue match ``desired``.

        Implemented once here because the logic is the same for any backend: add what is
        missing, update what changed interval, remove what should no longer run.
        """
        current = self.scheduled_products()
        added = updated = removed = unchanged = 0

        for product_id, interval in desired.items():
            existing = current.get(product_id)
            if existing is None:
                self.schedule_product(product_id, interval)
                added += 1
            elif existing != interval:
                self.schedule_product(product_id, interval)
                updated += 1
            else:
                unchanged += 1

        for product_id in current.keys() - desired.keys():
            self.unschedule_product(product_id)
            removed += 1

        return ReconcileReport(
            added=added, updated=updated, removed=removed, unchanged=unchanged
        )
