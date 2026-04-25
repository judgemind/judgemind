"""Integration test for the ``every_n_merges`` trigger kind (issue #3374).

Asserts the daemon's ``_scheduled_skills_tick`` fires a synthetic agent
when N qualifying merges have happened since the row's
``last_triggered_at``, and does NOT fire before that threshold.

Uses the same fake-cursor fixture pattern as the other dispatcher tests.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Test fixtures — shared shape across the dispatcher test suite.
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.fetchall_queue: list[list[Any]] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if not self.fetch_queue:
            return None
        return self.fetch_queue.pop(0)

    def fetchall(self) -> list[Any]:
        if not self.fetchall_queue:
            return []
        return self.fetchall_queue.pop(0)


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _make_daemon(fake_conn: _FakeConnection) -> daemon.DispatcherDaemon:
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake-for-tests",
        tick_scheduler_seconds=30,
        tick_supervisor_seconds=120,
        log_level="DEBUG",
        version_sha="deadbee",
        host="test-host",
        pid=4242,
    )
    logger = logging.getLogger("dispatcher.test.every_n_merges")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = fake_conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


class TestEveryNMergesTrigger:
    """``trigger_kind = 'every_n_merges'`` — fire when N merges accrue."""

    def test_fires_when_count_meets_threshold(self) -> None:
        fake_conn = _FakeConnection()
        last_triggered = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
        # Row read: one enabled scheduled-skill row.
        fake_conn.cursor_instance.fetchall_queue = [
            [("audit", "/audit", "every_n_merges", "2", last_triggered)],
        ]
        # Subsequent fetchone calls: merge count, concurrency_cap, collision-check,
        # then post-INSERT lookups. We script just enough to exercise the path.
        fake_conn.cursor_instance.fetch_queue = [
            (3,),  # merge count since last_triggered = 3 → exceeds threshold 2
            ("5",),  # concurrency_cap
            (0,),  # active_agent_count
            None,  # collision check — no running same-phase agent
        ]

        d = _make_daemon(fake_conn)
        # Stub the ECS launch — return a fake task ARN so the spawn
        # path's success branch runs without hitting boto.
        d._launch_agent_ecs_task = (  # type: ignore[method-assign]
            lambda agent_id, issue_number: "arn:aws:ecs:us-west-2::task/abc"
        )

        summary = d._scheduled_skills_tick()

        assert summary["fires_succeeded"] == 1, summary
        assert summary["fires_skipped_cap"] == 0
        assert summary["fires_skipped_collision"] == 0
        # The synthetic agent INSERT should have happened.
        executed = fake_conn.cursor_instance.executed
        assert any(
            "INSERT INTO dispatcher.agents" in sql
            and ("scheduled_skill" in str(params) if params else False)
            for sql, params in executed
        ), "expected synthetic agent INSERT with kind=scheduled_skill"
        # The last_triggered_* UPDATE should have fired.
        assert any(
            "UPDATE dispatcher.scheduled_skills" in sql for sql, _ in executed
        ), "expected scheduled_skills UPDATE for last_triggered_*"

    def test_no_fire_when_count_below_threshold(self) -> None:
        fake_conn = _FakeConnection()
        last_triggered = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
        fake_conn.cursor_instance.fetchall_queue = [
            [("audit", "/audit", "every_n_merges", "5", last_triggered)],
        ]
        fake_conn.cursor_instance.fetch_queue = [
            (3,),  # merge count = 3, below threshold 5
        ]

        d = _make_daemon(fake_conn)
        # Launch should never be called.
        called = []
        d._launch_agent_ecs_task = (  # type: ignore[method-assign]
            lambda agent_id, issue_number: called.append(agent_id) or "arn:irrelevant"
        )

        summary = d._scheduled_skills_tick()

        assert summary["fires_succeeded"] == 0
        assert summary["rows_scanned"] == 1
        assert called == []
        executed = fake_conn.cursor_instance.executed
        assert not any("INSERT INTO dispatcher.agents" in sql for sql, _ in executed), (
            "must not INSERT a synthetic agent below threshold"
        )

    def test_first_fire_with_null_last_triggered(self) -> None:
        """A fresh row (last_triggered_at IS NULL) counts all merges."""
        fake_conn = _FakeConnection()
        fake_conn.cursor_instance.fetchall_queue = [
            [("audit", "/audit", "every_n_merges", "1", None)],
        ]
        fake_conn.cursor_instance.fetch_queue = [
            (10,),  # 10 merges all-time, threshold = 1
            ("5",),  # cap
            (0,),  # active count
            None,  # collision check
        ]

        d = _make_daemon(fake_conn)
        d._launch_agent_ecs_task = (  # type: ignore[method-assign]
            lambda agent_id, issue_number: "arn:aws:ecs:us-west-2::task/x"
        )

        summary = d._scheduled_skills_tick()
        assert summary["fires_succeeded"] == 1

    def test_zero_threshold_never_fires(self) -> None:
        """Trigger value <= 0 is treated as "never fire" (defensive)."""
        fake_conn = _FakeConnection()
        last = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
        fake_conn.cursor_instance.fetchall_queue = [
            [("audit", "/audit", "every_n_merges", "0", last)],
        ]
        d = _make_daemon(fake_conn)
        summary = d._scheduled_skills_tick()
        assert summary["fires_succeeded"] == 0
        assert summary["fires_skipped_cap"] == 0
