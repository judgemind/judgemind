"""Unit tests for issue #3091 — ``_reap_completed_agent_tasks`` observes per-agent ECS lifecycle.

Coverage:

* STOPPED-success + terminal DB row -> log-only reap (no double
  terminal write).
* STOPPED-success + non-terminal DB row -> row gap; daemon marks the
  agent ``succeeded`` so the admin page clears.
* STOPPED-failure -> routed through ``_handle_agent_failure`` with
  category ``agent_task_stopped_unexpectedly``.
* RUNNING / PENDING -> no-op; still_running counter increments.
* Fresh-daemon-resumes-observation semantics (the #3078 Option A
  payoff): a new daemon run_id observing the same ARNs does NOT
  mark them terminal — it just re-reads their live status.
* No ECS cluster wired -> log and early return (preserve subprocess
  path).
* Empty active-rows set -> zero-work early return.

Fakes follow the pattern used by ``test_daemon_circuit_breaker.py``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# Only install a psycopg stub if one isn't already present — avoids
# clobbering sibling test files' UniqueViolation sentinel. See
# ``test_daemon_baseline_fetch_retry.py`` for the pattern.
if "psycopg" not in sys.modules or not isinstance(
    getattr(sys.modules["psycopg"].errors, "UniqueViolation", None), type
):

    class _UniqueViolation(Exception):
        pass

    _psycopg_stub = MagicMock()
    _psycopg_errors = MagicMock()
    _psycopg_errors.UniqueViolation = _UniqueViolation
    _psycopg_stub.errors = _psycopg_errors
    sys.modules["psycopg"] = _psycopg_stub

from dispatcher import daemon  # noqa: E402


# --------------------------------------------------------------------------
# Shared fakes.
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        # Queue of fetchone/fetchall results, consumed per-call.
        self.fetchone_queue: list[Any] = []
        self.fetchall_queue: list[list[Any]] = [rows] if rows is not None else []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        return self.fetchone_queue.pop(0) if self.fetchone_queue else None

    def fetchall(self) -> list[Any]:
        if self.fetchall_queue:
            return self.fetchall_queue.pop(0)
        return []


class _FakeConn:
    def __init__(self) -> None:
        self._cursors: list[_FakeCursor] = []
        self.committed = 0
        self.rolled_back = 0

    def queue_cursor(self, cur: _FakeCursor) -> None:
        self._cursors.append(cur)

    def cursor(self) -> _FakeCursor:
        if self._cursors:
            return self._cursors.pop(0)
        return _FakeCursor()

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


def _make_daemon(
    cluster_arn: str = "arn:aws:ecs:us-west-2:123:cluster/jm-dev",
) -> tuple[daemon.DispatcherDaemon, _FakeConn]:
    import threading

    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    conn = _FakeConn()
    d._main_conn = conn  # type: ignore[attr-defined]
    d._cfg = MagicMock(aws_region="us-west-2", ecs_cluster_arn=cluster_arn)  # type: ignore[attr-defined]
    d._log = logging.getLogger("test.daemon_reap")  # type: ignore[attr-defined]
    d._run_id = "test-run-id"  # type: ignore[attr-defined]
    d._ecs_client = None  # type: ignore[attr-defined]
    return d, conn


# --------------------------------------------------------------------------
# Happy-path reap transitions.
# --------------------------------------------------------------------------


class TestReapCompletedAgentTasks:
    def test_stopped_success_terminal_row_logs_only(self, caplog: Any) -> None:
        """STOPPED exit_code=0, row already ``succeeded`` -> reaped_success++."""
        d, conn = _make_daemon()
        caplog.set_level(logging.INFO, logger="test.daemon_reap")
        # First cursor: the active-agents SELECT returns one row.
        select_cur = _FakeCursor(
            rows=[
                (
                    "agent-ok",
                    101,
                    "arn:aws:ecs:us-west-2:123:task/jm/good",
                    "done",
                    "running",
                ),
            ]
        )
        conn.queue_cursor(select_cur)
        # Second cursor: _read_agent_status_phase returns (succeeded, done).
        status_cur = _FakeCursor()
        status_cur.fetchone_queue = [("succeeded", "done")]
        conn.queue_cursor(status_cur)

        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:us-west-2:123:task/jm/good",
                    "lastStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "containers": [{"exitCode": 0}],
                }
            ]
        }
        with patch.object(d, "_make_ecs_client", return_value=fake_client):
            summary = d._reap_completed_agent_tasks()

        assert summary == {
            "active": 1,
            "reaped_success": 1,
            "reaped_failure": 0,
            "still_running": 0,
        }
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_reaped_success" in events

    def test_stopped_success_row_gap_marks_succeeded(self, caplog: Any) -> None:
        """Container exited 0 but DB row still ``running`` -> daemon closes the gap."""
        d, conn = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_reap")
        select_cur = _FakeCursor(
            rows=[
                (
                    "agent-gap",
                    11,
                    "arn:aws:ecs:us-west-2:123:task/gap",
                    "verify",
                    "running",
                ),
            ]
        )
        conn.queue_cursor(select_cur)
        status_cur = _FakeCursor()
        status_cur.fetchone_queue = [("running", "verify")]
        conn.queue_cursor(status_cur)

        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:us-west-2:123:task/gap",
                    "lastStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "containers": [{"exitCode": 0}],
                }
            ]
        }
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_mark_agent_terminal") as mark_mock,
        ):
            summary = d._reap_completed_agent_tasks()

        assert summary["reaped_success"] == 1
        mark_mock.assert_called_once()
        kwargs = mark_mock.call_args.kwargs
        assert kwargs.get("status") == "succeeded"
        assert kwargs.get("exit_code") == 0
        assert kwargs.get("issue_number") == 11
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_reaped_success_row_gap" in events

    def test_stopped_failure_routes_to_handle_agent_failure(self, caplog: Any) -> None:
        d, conn = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_reap")
        select_cur = _FakeCursor(
            rows=[
                (
                    "agent-bad",
                    200,
                    "arn:aws:ecs:us-west-2:123:task/bad",
                    "ralph",
                    "running",
                ),
            ]
        )
        conn.queue_cursor(select_cur)
        status_cur = _FakeCursor()
        status_cur.fetchone_queue = [("running", "ralph")]
        conn.queue_cursor(status_cur)

        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:us-west-2:123:task/bad",
                    "lastStatus": "STOPPED",
                    "stopCode": "ContainerRuntimeError",
                    "containers": [{"exitCode": 137}],
                }
            ]
        }
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_handle_agent_failure") as fail_mock,
        ):
            summary = d._reap_completed_agent_tasks()

        assert summary["reaped_failure"] == 1
        fail_mock.assert_called_once()
        kw = fail_mock.call_args.kwargs
        assert kw["agent_id"] == "agent-bad"
        assert kw["category"] == "agent_task_stopped_unexpectedly"
        assert kw["exit_code"] == 137
        assert kw["details"]["stop_code"] == "ContainerRuntimeError"
        assert kw["details"]["task_arn"] == "arn:aws:ecs:us-west-2:123:task/bad"
        assert kw["issue_number"] == 200
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_reaped_failure" in events

    def test_stopped_failure_already_terminal_skips_double_route(
        self, caplog: Any
    ) -> None:
        """Don't re-insert a failure row if agent-runner wrote its own terminal."""
        d, conn = _make_daemon()
        caplog.set_level(logging.INFO, logger="test.daemon_reap")
        select_cur = _FakeCursor(
            rows=[
                (
                    "agent-done",
                    300,
                    "arn:aws:ecs:us-west-2:123:task/done",
                    "ralph",
                    "running",
                ),
            ]
        )
        conn.queue_cursor(select_cur)
        status_cur = _FakeCursor()
        # Agent-runner wrote ``failed`` before exiting.
        status_cur.fetchone_queue = [("failed", "ralph")]
        conn.queue_cursor(status_cur)

        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:us-west-2:123:task/done",
                    "lastStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "containers": [{"exitCode": 2}],  # non-zero exit
                }
            ]
        }
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_handle_agent_failure") as fail_mock,
        ):
            summary = d._reap_completed_agent_tasks()
        assert summary["reaped_failure"] == 1
        fail_mock.assert_not_called()
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_reaped_failure_already_terminal" in events

    def test_running_task_is_noop(self) -> None:
        d, conn = _make_daemon()
        select_cur = _FakeCursor(
            rows=[
                (
                    "agent-live",
                    400,
                    "arn:aws:ecs:us-west-2:123:task/live",
                    "ralph",
                    "running",
                ),
            ]
        )
        conn.queue_cursor(select_cur)

        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:us-west-2:123:task/live",
                    "lastStatus": "RUNNING",
                    "containers": [],
                }
            ]
        }
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_mark_agent_terminal") as mark_mock,
            patch.object(d, "_handle_agent_failure") as fail_mock,
        ):
            summary = d._reap_completed_agent_tasks()
        # No action — agent is alive.
        mark_mock.assert_not_called()
        fail_mock.assert_not_called()
        assert summary == {
            "active": 1,
            "reaped_success": 0,
            "reaped_failure": 0,
            "still_running": 1,
        }


# --------------------------------------------------------------------------
# Fresh-daemon-resumes-observation — the #3078 Option A payoff.
# --------------------------------------------------------------------------


class TestFreshDaemonResumesObservation:
    def test_fresh_run_id_observes_in_flight_without_abandonment(self) -> None:
        """A brand-new daemon run sees in-flight agent-runner tasks as RUNNING
        and leaves them alone. The pre-#3091 subprocess model marked every
        in-flight agent as ``daemon_restart_abandoned`` on restart; this
        test documents that the ECS-path reap does NOT do that.
        """
        d, conn = _make_daemon()
        # The agent was launched by a PREVIOUS daemon run (run_id='old').
        # This daemon has run_id='test-run-id' (from _make_daemon).
        assert d._run_id == "test-run-id"
        select_cur = _FakeCursor(
            rows=[
                (
                    "agent-surv",
                    50,
                    "arn:aws:ecs:us-west-2:123:task/surv",
                    "ralph",
                    "running",
                ),
            ]
        )
        conn.queue_cursor(select_cur)
        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:us-west-2:123:task/surv",
                    "lastStatus": "RUNNING",
                    "containers": [],
                }
            ]
        }
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_mark_agent_terminal") as mark_mock,
            patch.object(d, "_handle_agent_failure") as fail_mock,
        ):
            summary = d._reap_completed_agent_tasks()
        mark_mock.assert_not_called()
        fail_mock.assert_not_called()
        assert summary["still_running"] == 1
        assert summary["reaped_success"] == 0
        assert summary["reaped_failure"] == 0


# --------------------------------------------------------------------------
# Guards — empty rows, missing cluster, describe failure.
# --------------------------------------------------------------------------


class TestReapGuards:
    def test_no_active_rows_early_returns(self) -> None:
        d, conn = _make_daemon()
        select_cur = _FakeCursor(rows=[])
        conn.queue_cursor(select_cur)
        with patch.object(d, "_make_ecs_client") as mock_make:
            summary = d._reap_completed_agent_tasks()
        # Never built the ECS client — no work to do.
        mock_make.assert_not_called()
        assert summary == {
            "active": 0,
            "reaped_success": 0,
            "reaped_failure": 0,
            "still_running": 0,
        }

    def test_missing_cluster_logs_and_returns(self, caplog: Any) -> None:
        d, conn = _make_daemon(cluster_arn="")
        caplog.set_level(logging.WARNING, logger="test.daemon_reap")
        select_cur = _FakeCursor(
            rows=[
                ("agent-a", 1, "arn:aws:ecs:us-west-2:123:task/a", "ralph", "running"),
            ]
        )
        conn.queue_cursor(select_cur)
        with patch.object(d, "_make_ecs_client") as mock_make:
            summary = d._reap_completed_agent_tasks()
        mock_make.assert_not_called()
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "reap_agent_tasks_no_cluster" in events
        assert summary["active"] == 1

    def test_describe_tasks_exception_is_logged_not_raised(self, caplog: Any) -> None:
        d, conn = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon_reap")
        select_cur = _FakeCursor(
            rows=[
                ("agent-a", 1, "arn:aws:ecs:us-west-2:123:task/a", "ralph", "running"),
            ]
        )
        conn.queue_cursor(select_cur)
        fake_client = MagicMock()
        fake_client.describe_tasks.side_effect = RuntimeError("endpoint outage")
        with patch.object(d, "_make_ecs_client", return_value=fake_client):
            summary = d._reap_completed_agent_tasks()
        # Does not raise; returns a neutral summary so the scheduler
        # tick keeps ticking.
        assert summary["still_running"] == 0
        assert summary["reaped_success"] == 0
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "reap_agent_tasks_describe_failed" in events
