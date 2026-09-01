"""Worker liveness, measured from the heartbeat table.

Replaces the earlier inference from overdue jobs, which could not tell "no worker" from
"a worker running but wedged", and cried wolf whenever a check legitimately ran long.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from tests.unit.test_scheduler import FakeQueue

from product_tracker.core.config import get_settings
from product_tracker.db.models import WorkerHeartbeat
from product_tracker.db.session import get_engine, session_scope
from product_tracker.scheduler import heartbeat
from product_tracker.scheduler.runner import WorkerRunner

pytestmark = pytest.mark.db

INTERVAL = 60


def clear_heartbeats() -> None:
    with get_engine().begin() as connection:
        connection.execute(text("DELETE FROM worker_heartbeats"))


def read() -> heartbeat.WorkerLiveness:
    with session_scope() as session:
        return heartbeat.read(session, reconcile_interval_seconds=INTERVAL)


class TestTouch:
    def test_nothing_reported_yet(self, db_env: None) -> None:
        clear_heartbeats()

        liveness = read()

        assert liveness.available
        assert liveness.running is None
        assert "has ever reported in" in liveness.detail

    def test_a_beat_makes_the_worker_alive(self, db_env: None) -> None:
        clear_heartbeats()
        with session_scope() as session:
            heartbeat.touch(session, "worker-a")

        liveness = read()

        assert liveness.running is True
        assert liveness.workers == 1

    def test_repeated_beats_do_not_duplicate_the_row(self, db_env: None) -> None:
        """Upsert on worker_id: a restart replaces its own row."""
        clear_heartbeats()
        for _ in range(3):
            with session_scope() as session:
                heartbeat.touch(session, "worker-a")

        assert read().workers == 1

    def test_two_workers_are_both_visible(self, db_env: None) -> None:
        """Running two is a misconfiguration, and hiding it would be worse."""
        clear_heartbeats()
        with session_scope() as session:
            heartbeat.touch(session, "worker-a")
            heartbeat.touch(session, "worker-b")

        assert read().workers == 2

    def test_records_hostname_and_pid(self, db_env: None) -> None:
        clear_heartbeats()
        with session_scope() as session:
            heartbeat.touch(session, "worker-a")

        with session_scope() as session:
            row = session.query(WorkerHeartbeat).one()
        assert row.hostname
        assert row.pid


class TestStaleness:
    def _beat_at(self, worker_id: str, seconds_ago: int) -> None:
        moment = datetime.now(UTC) - timedelta(seconds=seconds_ago)
        with get_engine().begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO worker_heartbeats (worker_id, hostname, pid, started_at,"
                    " last_seen_at) VALUES (:w, 'h', 1, :t, :t)"
                ),
                {"w": worker_id, "t": moment},
            )

    def test_a_recent_beat_is_alive(self, db_env: None) -> None:
        clear_heartbeats()
        self._beat_at("worker-a", 5)

        assert read().running is True

    def test_an_old_beat_is_stale(self, db_env: None) -> None:
        clear_heartbeats()
        self._beat_at("worker-a", heartbeat.stale_after(INTERVAL) + 60)

        liveness = read()

        assert liveness.running is False
        assert "over the" in liveness.detail

    def test_one_missed_beat_is_tolerated(self, db_env: None) -> None:
        """A slow check is not a dead worker."""
        clear_heartbeats()
        self._beat_at("worker-a", INTERVAL + 5)

        assert read().running is True

    def test_the_window_scales_with_the_reconcile_interval(self) -> None:
        assert heartbeat.stale_after(600) > heartbeat.stale_after(60)

    def test_there_is_a_floor(self) -> None:
        """A very short reconcile interval must not make this hair-trigger."""
        assert heartbeat.stale_after(1) == heartbeat.MIN_STALE_SECONDS


class TestPruneAndClear:
    def test_clear_removes_only_this_worker(self, db_env: None) -> None:
        clear_heartbeats()
        with session_scope() as session:
            heartbeat.touch(session, "worker-a")
            heartbeat.touch(session, "worker-b")

        with session_scope() as session:
            heartbeat.clear(session, "worker-a")

        assert read().workers == 1

    def test_clear_is_silent_for_an_unknown_worker(self, db_env: None) -> None:
        clear_heartbeats()
        with session_scope() as session:
            heartbeat.clear(session, "never-existed")

    def test_prune_drops_only_the_ancient(self, db_env: None) -> None:
        """After a kill -9 there is no clean shutdown to remove the row."""
        clear_heartbeats()
        with session_scope() as session:
            heartbeat.touch(session, "fresh")
        TestStaleness()._beat_at("ancient", 10_000)

        with session_scope() as session:
            removed = heartbeat.prune(session, older_than_seconds=5_000)

        assert removed == 1
        assert read().workers == 1


class TestRunnerIntegration:
    def test_setup_beats_and_stop_clears(self, db_env: None) -> None:
        clear_heartbeats()
        runner = WorkerRunner(get_settings(), queue=FakeQueue())

        runner.setup()
        assert read().running is True

        runner.stop()

        # A deliberately stopped worker reads as stopped at once, not once it goes stale.
        assert read().running is None

    def test_each_runner_gets_its_own_id(self, db_env: None) -> None:
        first = WorkerRunner(get_settings(), queue=FakeQueue())
        second = WorkerRunner(get_settings(), queue=FakeQueue())

        assert first.worker_id != second.worker_id

    def test_a_missing_table_is_reported_not_raised(self, db_env: None) -> None:
        from sqlalchemy.orm import Session

        connection = get_engine().connect()
        transaction = connection.begin()
        try:
            connection.execute(text("DROP TABLE worker_heartbeats"))
            liveness = heartbeat.read(
                Session(bind=connection), reconcile_interval_seconds=INTERVAL
            )
        finally:
            transaction.rollback()
            connection.close()

        assert not liveness.available
        assert "alembic upgrade" in liveness.detail
