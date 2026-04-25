"""Cap-aware test for ``_scheduled_skills_tick`` (issue #3374).

Asserts the daemon does NOT fire a scheduled-skill row when
``_active_agent_count() >= concurrency_cap``. The skill is requeued
implicitly — the next supervisor tick re-checks.
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

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


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
    logger = logging.getLogger("dispatcher.test.cap_aware")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = fake_conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d


class TestCapAware:
    def test_no_fire_when_cap_zero(self) -> None:
        """``concurrency_cap=0`` (killswitch) blocks every fire."""
        fake_conn = _FakeConnection()
        last = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
        fake_conn.cursor_instance.fetchall_queue = [
            [("audit", "/audit", "every_n_merges", "1", last)],
        ]
        fake_conn.cursor_instance.fetch_queue = [
            (10,),  # merge count exceeds threshold
            ("0",),  # concurrency_cap = 0
            (0,),  # active_agent_count
        ]

        d = _make_daemon(fake_conn)
        called = []
        d._launch_agent_ecs_task = (  # type: ignore[method-assign]
            lambda agent_id, issue_number: called.append(agent_id) or "arn"
        )

        summary = d._scheduled_skills_tick()

        assert summary["fires_succeeded"] == 0
        assert summary["fires_skipped_cap"] == 1
        assert called == [], "ECS launch must NOT happen when cap=0"

    def test_no_fire_when_active_equals_cap(self) -> None:
        """When ``active >= cap``, scheduled-skill fires must wait."""
        fake_conn = _FakeConnection()
        last = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
        fake_conn.cursor_instance.fetchall_queue = [
            [("audit", "/audit", "every_n_merges", "1", last)],
        ]
        fake_conn.cursor_instance.fetch_queue = [
            (10,),  # merges exceed
            ("2",),  # cap = 2
            (2,),  # active = 2 — exactly at cap
        ]

        d = _make_daemon(fake_conn)
        called = []
        d._launch_agent_ecs_task = (  # type: ignore[method-assign]
            lambda agent_id, issue_number: called.append(agent_id) or "arn"
        )

        summary = d._scheduled_skills_tick()
        assert summary["fires_skipped_cap"] == 1
        assert summary["fires_succeeded"] == 0
        assert called == []

    def test_fire_when_below_cap(self) -> None:
        """``active < cap`` lets the fire proceed."""
        fake_conn = _FakeConnection()
        last = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
        fake_conn.cursor_instance.fetchall_queue = [
            [("audit", "/audit", "every_n_merges", "1", last)],
        ]
        fake_conn.cursor_instance.fetch_queue = [
            (10,),  # merges exceed
            ("5",),  # cap = 5
            (1,),  # active = 1 — well below cap
            None,  # collision check (no running same-phase)
        ]

        d = _make_daemon(fake_conn)
        d._launch_agent_ecs_task = (  # type: ignore[method-assign]
            lambda agent_id, issue_number: "arn:aws:ecs:us-west-2::task/y"
        )

        summary = d._scheduled_skills_tick()
        assert summary["fires_succeeded"] == 1
        assert summary["fires_skipped_cap"] == 0
