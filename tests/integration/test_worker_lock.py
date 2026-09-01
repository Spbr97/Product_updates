"""Only one worker per database, enforced rather than documented.

Two workers against one database each run every job: every product checked twice, every
store hit twice as hard, and the only symptom is duplicated rows that look like a bug
somewhere else. An advisory lock turns that silent doubling into a loud refusal.
"""

from __future__ import annotations

import pytest
from tests.unit.test_scheduler import FakeQueue

from product_tracker.core.config import get_settings
from product_tracker.scheduler.lock import (
    WORKER_LOCK_KEY,
    WorkerAlreadyRunningError,
    WorkerLock,
)
from product_tracker.scheduler.runner import WorkerRunner

pytestmark = pytest.mark.db


class TestAcquire:
    def test_the_first_worker_gets_the_lock(self, db_env: None) -> None:
        lock = WorkerLock()
        try:
            assert lock.acquire() is True
        finally:
            lock.release()

    def test_a_second_worker_is_refused(self, db_env: None) -> None:
        first, second = WorkerLock(), WorkerLock()
        try:
            assert first.acquire() is True
            assert second.acquire() is False
        finally:
            second.release()
            first.release()

    def test_the_lock_is_reusable_after_release(self, db_env: None) -> None:
        first = WorkerLock()
        first.acquire()
        first.release()

        second = WorkerLock()
        try:
            assert second.acquire() is True
        finally:
            second.release()

    def test_release_without_acquire_is_safe(self, db_env: None) -> None:
        WorkerLock().release()

    def test_double_release_is_safe(self, db_env: None) -> None:
        lock = WorkerLock()
        lock.acquire()
        lock.release()
        lock.release()

    def test_different_keys_do_not_collide(self, db_env: None) -> None:
        """Sanity check that the lock is keyed, not global."""
        first, second = WorkerLock(), WorkerLock(key=WORKER_LOCK_KEY + 1)
        try:
            assert first.acquire() is True
            assert second.acquire() is True
        finally:
            second.release()
            first.release()


class TestContextManager:
    def test_acquires_and_releases(self, db_env: None) -> None:
        with WorkerLock():
            assert WorkerLock().acquire() is False

        after = WorkerLock()
        try:
            assert after.acquire() is True
        finally:
            after.release()

    def test_a_second_entry_raises(self, db_env: None) -> None:
        second = WorkerLock()

        with WorkerLock(), pytest.raises(WorkerAlreadyRunningError) as excinfo:
            second.__enter__()

        assert "another worker is already running" in str(excinfo.value)

    def test_the_error_explains_the_consequence(self, db_env: None) -> None:
        """Someone hitting this needs to know why it matters, not just that it happened."""
        with WorkerLock(), pytest.raises(WorkerAlreadyRunningError) as excinfo:
            WorkerLock().__enter__()

        assert "every job" in str(excinfo.value)

    def test_the_lock_is_released_when_the_body_raises(self, db_env: None) -> None:
        with pytest.raises(ValueError), WorkerLock():
            raise ValueError("boom")

        after = WorkerLock()
        try:
            assert after.acquire() is True
        finally:
            after.release()


class TestRunnerRefusal:
    def test_a_second_runner_refuses_to_start(self, db_env: None) -> None:
        runner = WorkerRunner(get_settings(), queue=FakeQueue())

        with WorkerLock(), pytest.raises(WorkerAlreadyRunningError):
            runner.run()

    def test_nothing_was_scheduled_by_the_refused_runner(self, db_env: None) -> None:
        """It must decline before touching the schedule."""
        queue = FakeQueue()
        runner = WorkerRunner(get_settings(), queue=queue)

        with WorkerLock(), pytest.raises(WorkerAlreadyRunningError):
            runner.run()

        assert queue.jobs == {}
        assert queue.started is False
