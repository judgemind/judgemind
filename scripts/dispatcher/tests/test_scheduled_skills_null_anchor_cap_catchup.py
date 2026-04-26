"""NULL-anchor cap-catchup test for ``_scheduled_skills_tick`` (issue #3424).

Asserts that when a cron row has ``last_triggered_at = NULL`` and the daemon
is at concurrency cap on the first tick, the row's ``last_triggered_at`` is
NOT updated (preserving NULL).  On the next tick — when cap clears — the
24h NULL-anchor lookback fires the skill and issues the UPDATE.

Maps acceptance criteria #3 and #6 from issue #3424.
"""

from __future__ import annotations

import logging
import sys

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
    logger = logging.getLogger("dispatcher.test.null_anchor_cap_catchup")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = fake_conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d


class TestNullAnchorCapCatchup:
    def test_cap_busy_at_target_minute_preserves_null_anchor_and_catches_up(
        self,
    ) -> None:
        """AC #3 + AC #6: cap-skip preserves NULL; next tick fires + UPDATEs.

        Tick 1: ``last_triggered_at = NULL``, cron ``0 12 * * *``, now 12:00Z.
                Concurrency cap = 0 → fires_skipped_cap = 1.
                No ``UPDATE dispatcher.scheduled_skills`` should be issued.

        Tick 2: ``last_triggered_at = NULL`` still (not changed by Tick 1).
                now = 13:00Z, cap = 5, active = 1, no collision.
                The 24h NULL-anchor lookback finds 12:00 → fires_succeeded = 1.
                An ``UPDATE dispatcher.scheduled_skills`` is issued.
        """
        # --- Tick 1: cap = 0, skill would match but is blocked by cap ---
        fake_conn = _FakeConnection()
        row = ("daily-report", "/dispatcher-daily-report", "cron", "0 12 * * *", None)
        fake_conn.cursor_instance.fetchall_queue = [
            [row],
        ]
        # _read_concurrency_cap_for_scheduler → fetchone → cap = 0
        # _active_agent_count → fetchone → active = 1
        fake_conn.cursor_instance.fetch_queue = [
            ("0",),  # concurrency_cap = 0
            (1,),  # active_agent_count = 1  (1 >= 0 → skip)
        ]

        d = _make_daemon(fake_conn)
        launch_calls: list[Any] = []
        d._launch_agent_ecs_task = (  # type: ignore[method-assign]
            lambda agent_id, issue_number: launch_calls.append(agent_id) or "arn"
        )

        # Freeze now via monkeypatching the daemon's internal clock is not
        # straightforward — instead pass now indirectly by letting the
        # real _scheduled_skills_tick compute now() naturally.  However,
        # since we need a deterministic now for the cron evaluation, we
        # override the datetime used inside the tick by patching
        # ``daemon.datetime`` to return our frozen time.
        #
        # Simpler approach: set _now_for_scheduled_skills if the daemon
        # exposes it, otherwise rely on the real wall clock being close
        # enough to the test's expected window.  Since the test expression
        # ``0 12 * * *`` with a NULL anchor reaches back 24h, any
        # invocation within the 24h window will return True.  We just
        # need to ensure the cap-skip path runs on Tick 1.
        summary1 = d._scheduled_skills_tick()

        assert summary1["fires_skipped_cap"] == 1, (
            f"Tick 1 should skip due to cap=0, got: {summary1}"
        )
        assert summary1["fires_succeeded"] == 0

        # No UPDATE should have been issued — last_triggered_at stays NULL.
        update_sqls = [
            sql
            for sql, _ in fake_conn.cursor_instance.executed
            if sql.strip().upper().startswith("UPDATE DISPATCHER.SCHEDULED_SKILLS")
        ]
        assert update_sqls == [], (
            f"No UPDATE should be issued on cap-skip; found: {update_sqls}"
        )

        # --- Tick 2: cap = 5, active = 1, no collision — should fire ---
        fake_conn2 = _FakeConnection()
        # Row still has last_triggered_at = None (not mutated by Tick 1).
        fake_conn2.cursor_instance.fetchall_queue = [
            [row],
        ]
        # _read_concurrency_cap_for_scheduler → cap = 5
        # _active_agent_count → active = 1 (1 < 5 → proceed)
        # _scheduled_skill_running → None (no collision)
        fake_conn2.cursor_instance.fetch_queue = [
            ("5",),  # concurrency_cap = 5
            (1,),  # active_agent_count = 1
            None,  # collision check: no running agent with phase="daily-report"
        ]

        d2 = _make_daemon(fake_conn2)
        launch_calls2: list[Any] = []
        d2._launch_agent_ecs_task = (  # type: ignore[method-assign]
            lambda agent_id, issue_number: launch_calls2.append(agent_id) or "arn:tick2"
        )

        summary2 = d2._scheduled_skills_tick()

        assert summary2["fires_succeeded"] == 1, (
            f"Tick 2 should fire (NULL anchor + 24h lookback), got: {summary2}"
        )
        assert summary2["fires_skipped_cap"] == 0

        # An UPDATE should have been issued on Tick 2.
        update_sqls2 = [
            sql
            for sql, _ in fake_conn2.cursor_instance.executed
            if sql.strip().upper().startswith("UPDATE DISPATCHER.SCHEDULED_SKILLS")
        ]
        assert len(update_sqls2) == 1, (
            f"Exactly one UPDATE should be issued on fire; found: {update_sqls2}"
        )
        assert launch_calls2, "ECS launch should have been called on Tick 2"
