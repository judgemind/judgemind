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
        d._read_agent_pr_number = lambda agent_id: 101  # type: ignore[method-assign]
        d._fetch_pr_status = lambda pr_number: {
            "state": "MERGED",
            "mergedAt": "2026-04-29T12:00:00Z",
        }  # type: ignore[method-assign]
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
        # Third cursor: _reap_finalize_ecs_success merged_at UPDATE.
        finalize_cur = _FakeCursor()
        conn.queue_cursor(finalize_cur)
        # Fourth cursor: _reap_finalize_ecs_success agent_task_arn UPDATE
        # (Issue #3329 — clear the ARN so the next tick excludes the row).
        arn_clear_cur = _FakeCursor()
        conn.queue_cursor(arn_clear_cur)

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
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_gh_issue_remove_labels") as remove_labels_mock,
        ):
            summary = d._reap_completed_agent_tasks()

        assert summary == {
            "active": 1,
            "reaped_success": 1,
            "reaped_failure": 0,
            "still_running": 0,
            "reaped_untracked": 0,
        }
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_reaped_success" in events
        # Issue #3324: success-already-terminal must strip
        # ``status/in-progress`` (claim-interlock teardown) and stamp
        # ``merged_at`` (fleet-status visibility).
        remove_labels_mock.assert_called_once_with(
            101, [daemon.STATUS_IN_PROGRESS_LABEL]
        )
        # The merged_at UPDATE must be issued and gated by the
        # idempotence WHERE filters.
        sqls = [sql for sql, _ in finalize_cur.executed]
        assert any("UPDATE dispatcher.agents" in sql for sql in sqls)
        merged_at_sql = next(sql for sql in sqls if "merged_at" in sql)
        assert "merged_at IS NULL" in merged_at_sql
        assert "pr_number IS NOT NULL" in merged_at_sql
        params = [params for _, params in finalize_cur.executed]
        assert params == [("agent-ok",)]
        # Issue #3329: success path must also clear ``agent_task_arn``
        # so the next reaper tick's SELECT (now keyed solely on
        # ``agent_task_arn IS NOT NULL``) skips this row.
        arn_sqls = [sql for sql, _ in arn_clear_cur.executed]
        assert any("SET agent_task_arn = NULL" in sql for sql in arn_sqls), arn_sqls
        assert [params for _, params in arn_clear_cur.executed] == [("agent-ok",)]

    def test_stopped_success_already_terminal_idempotent_re_reap(
        self, caplog: Any
    ) -> None:
        """Issue #3324: a second reap on an already-merged_at row must NOT
        bump the timestamp.

        The helper relies on the WHERE filter (``merged_at IS NULL AND
        pr_number IS NOT NULL``) to make the UPDATE a no-op when the row
        is already finalized; we assert the WHERE clause is present so
        a future refactor can't silently strip it.
        """
        d, conn = _make_daemon()
        d._read_agent_pr_number = lambda agent_id: 102  # type: ignore[method-assign]
        d._fetch_pr_status = lambda pr_number: {
            "state": "MERGED",
            "mergedAt": "2026-04-29T12:00:00Z",
        }  # type: ignore[method-assign]
        caplog.set_level(logging.INFO, logger="test.daemon_reap")
        select_cur = _FakeCursor(
            rows=[
                (
                    "agent-rerun",
                    102,
                    "arn:aws:ecs:us-west-2:123:task/jm/rerun",
                    "done",
                    "running",
                ),
            ]
        )
        conn.queue_cursor(select_cur)
        status_cur = _FakeCursor()
        status_cur.fetchone_queue = [("succeeded", "done")]
        conn.queue_cursor(status_cur)
        finalize_cur = _FakeCursor()
        conn.queue_cursor(finalize_cur)
        arn_clear_cur = _FakeCursor()
        conn.queue_cursor(arn_clear_cur)

        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:us-west-2:123:task/jm/rerun",
                    "lastStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "containers": [{"exitCode": 0}],
                }
            ]
        }
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_gh_issue_remove_labels"),
        ):
            d._reap_completed_agent_tasks()

        # Exactly one merged_at UPDATE issued. The WHERE filter
        # (asserted above) makes the SQL itself a no-op when merged_at
        # is already set — the DB does the idempotence; the helper just
        # always issues the same statement.
        merged_at_sqls = [sql for sql, _ in finalize_cur.executed if "merged_at" in sql]
        assert len(merged_at_sqls) == 1, merged_at_sqls
        assert "merged_at IS NULL" in merged_at_sqls[0]
        assert "pr_number IS NOT NULL" in merged_at_sqls[0]

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
        # Issue #3329: failure-already-terminal branch also issues the
        # ``agent_task_arn = NULL`` UPDATE to take the row out of the
        # next reaper SELECT.
        arn_clear_cur = _FakeCursor()
        conn.queue_cursor(arn_clear_cur)

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
            patch.object(d, "_gh_issue_remove_labels") as remove_labels_mock,
        ):
            summary = d._reap_completed_agent_tasks()
        assert summary["reaped_failure"] == 1
        fail_mock.assert_not_called()
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_reaped_failure_already_terminal" in events
        # Issue #3324: failure-already-terminal must also strip
        # ``status/in-progress`` (mirrors subprocess-mode failure
        # teardown). No ``merged_at`` stamp on the failure path.
        remove_labels_mock.assert_called_once_with(
            300, [daemon.STATUS_IN_PROGRESS_LABEL]
        )
        # Issue #3329: ``agent_task_arn`` must be cleared on the
        # failure path too, otherwise the row stays in the SELECT
        # forever now that the status filter is gone.
        arn_sqls = [sql for sql, _ in arn_clear_cur.executed]
        assert any("SET agent_task_arn = NULL" in sql for sql in arn_sqls), arn_sqls
        assert [params for _, params in arn_clear_cur.executed] == [("agent-done",)]

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
            "reaped_untracked": 0,
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
            "reaped_untracked": 0,
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


# --------------------------------------------------------------------------
# Issue #3329 — SELECT must NOT filter on ``status`` (the agent-runner
# entrypoint writes its terminal status BEFORE ECS marks the task STOPPED,
# so a status filter excludes every real ECS-mode terminal). The fix:
# - drop the ``status IN ('running', 'retrying')`` filter from the SELECT,
# - clear ``agent_task_arn`` after success/failure finalize so the next
#   tick excludes the row instead.
# --------------------------------------------------------------------------


class TestReapSelectScope:
    def test_select_query_does_not_filter_by_status(self) -> None:
        """The SELECT must key on ``agent_task_arn IS NOT NULL`` alone.

        Pre-fix the WHERE clause had ``AND status IN ('running',
        'retrying')``. The agent-runner (#3158) writes its own terminal
        ``status='succeeded'`` row before exit, which transitions out of
        the filter window before the ECS task transitions to STOPPED —
        so the rows were excluded and the success/failure already-
        terminal branches never fired in production. This test pins
        the SELECT shape against regression.
        """
        d, conn = _make_daemon()
        select_cur = _FakeCursor(rows=[])
        conn.queue_cursor(select_cur)
        with patch.object(d, "_make_ecs_client"):
            d._reap_completed_agent_tasks()

        assert len(select_cur.executed) == 1
        sql, _params = select_cur.executed[0]
        assert "FROM dispatcher.agents" in sql
        assert "agent_task_arn IS NOT NULL" in sql
        # Regression guard: the status filter must stay gone (#3329).
        assert "status IN" not in sql, sql
        assert "running" not in sql, sql
        assert "retrying" not in sql, sql

    def test_terminal_status_row_is_still_reaped(self, caplog: Any) -> None:
        """A row whose ``status`` is already terminal but still has
        ``agent_task_arn`` set MUST be picked up by the reaper. This is
        the production-realistic shape — the agent-runner writes
        ``status='succeeded'`` before the ECS task STOPS, so the row is
        ALREADY terminal by the time the daemon's reaper SELECTs it.

        Pre-fix this row was excluded by the ``status IN ('running',
        'retrying')`` filter. Post-fix the daemon describes the task,
        sees STOPPED+exit0, and runs the success-already-terminal
        finalize branch (label strip + merged_at + agent_task_arn
        clear).
        """
        d, conn = _make_daemon()
        d._read_agent_pr_number = lambda agent_id: 501  # type: ignore[method-assign]
        d._fetch_pr_status = lambda pr_number: {
            "state": "MERGED",
            "mergedAt": "2026-04-29T12:00:00Z",
        }  # type: ignore[method-assign]
        caplog.set_level(logging.INFO, logger="test.daemon_reap")
        select_cur = _FakeCursor(
            rows=[
                (
                    "agent-already-succeeded",
                    501,
                    "arn:aws:ecs:us-west-2:123:task/jm/already",
                    "done",
                    # Production-realistic: status is already terminal
                    # by the time this SELECT runs.
                    "succeeded",
                ),
            ]
        )
        conn.queue_cursor(select_cur)
        status_cur = _FakeCursor()
        status_cur.fetchone_queue = [("succeeded", "done")]
        conn.queue_cursor(status_cur)
        finalize_cur = _FakeCursor()
        conn.queue_cursor(finalize_cur)
        arn_clear_cur = _FakeCursor()
        conn.queue_cursor(arn_clear_cur)

        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:us-west-2:123:task/jm/already",
                    "lastStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "containers": [{"exitCode": 0}],
                }
            ]
        }
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_gh_issue_remove_labels") as remove_labels_mock,
        ):
            summary = d._reap_completed_agent_tasks()

        assert summary["reaped_success"] == 1
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_reaped_success" in events
        # Label stripped, merged_at stamped, agent_task_arn cleared.
        remove_labels_mock.assert_called_once_with(
            501, [daemon.STATUS_IN_PROGRESS_LABEL]
        )
        merged_sqls = [sql for sql, _ in finalize_cur.executed if "merged_at" in sql]
        assert merged_sqls and "merged_at IS NULL" in merged_sqls[0]
        arn_sqls = [sql for sql, _ in arn_clear_cur.executed]
        assert any("SET agent_task_arn = NULL" in sql for sql in arn_sqls), arn_sqls

    def test_failed_status_row_is_still_reaped(self, caplog: Any) -> None:
        """Mirror of the success case for the failure path.

        A row already in ``status='failed'`` but with ``agent_task_arn``
        still set must be picked up; the reaper should strip the label
        and clear ``agent_task_arn``. No ``merged_at`` stamp on the
        failure path.
        """
        d, conn = _make_daemon()
        caplog.set_level(logging.INFO, logger="test.daemon_reap")
        select_cur = _FakeCursor(
            rows=[
                (
                    "agent-already-failed",
                    777,
                    "arn:aws:ecs:us-west-2:123:task/jm/failterm",
                    "ralph",
                    # Production-realistic: status is already terminal.
                    "failed",
                ),
            ]
        )
        conn.queue_cursor(select_cur)
        status_cur = _FakeCursor()
        status_cur.fetchone_queue = [("failed", "ralph")]
        conn.queue_cursor(status_cur)
        arn_clear_cur = _FakeCursor()
        conn.queue_cursor(arn_clear_cur)

        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:us-west-2:123:task/jm/failterm",
                    # Issue scenario in the spec writeup:
                    # ECS reports STOPPED+exit0 even though the agent
                    # row is ``failed``. The exit-code-zero +
                    # status-is-terminal combination is treated as a
                    # success-already-terminal in the reaper logic
                    # (the agent-runner wrote its own terminal). This
                    # variant uses a non-zero stop_code to force the
                    # failure branch instead.
                    "lastStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "containers": [{"exitCode": 1}],  # non-zero → failure path
                }
            ]
        }
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_handle_agent_failure") as fail_mock,
            patch.object(d, "_gh_issue_remove_labels") as remove_labels_mock,
        ):
            summary = d._reap_completed_agent_tasks()

        assert summary["reaped_failure"] == 1
        # Already-terminal branch — must NOT route through
        # _handle_agent_failure (would double-insert a failures row).
        fail_mock.assert_not_called()
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_runner_reaped_failure_already_terminal" in events
        remove_labels_mock.assert_called_once_with(
            777, [daemon.STATUS_IN_PROGRESS_LABEL]
        )
        arn_sqls = [sql for sql, _ in arn_clear_cur.executed]
        assert any("SET agent_task_arn = NULL" in sql for sql in arn_sqls), arn_sqls
        assert [params for _, params in arn_clear_cur.executed] == [
            ("agent-already-failed",)
        ]


class TestReapArnClearIdempotence:
    def test_arn_null_row_is_not_reselected(self) -> None:
        """After finalize clears ``agent_task_arn``, the next reaper tick
        must skip the row entirely — no ECS DescribeTasks call, no
        UPDATEs, no log spam. The SELECT's ``agent_task_arn IS NOT NULL``
        clause is the gate, so an empty fetchall() simulates the
        post-finalize state.
        """
        d, conn = _make_daemon()
        # Empty result set models the post-clear DB state for any agent
        # already finalized: ``agent_task_arn IS NOT NULL`` excludes it.
        select_cur = _FakeCursor(rows=[])
        conn.queue_cursor(select_cur)

        with (
            patch.object(d, "_make_ecs_client") as mock_make_client,
            patch.object(d, "_gh_issue_remove_labels") as remove_labels_mock,
            patch.object(d, "_mark_agent_terminal") as mark_mock,
            patch.object(d, "_handle_agent_failure") as fail_mock,
        ):
            summary = d._reap_completed_agent_tasks()

        # Zero-work early return: no ECS client built, no GH calls,
        # no terminal-marking, no failure routing.
        mock_make_client.assert_not_called()
        remove_labels_mock.assert_not_called()
        mark_mock.assert_not_called()
        fail_mock.assert_not_called()
        assert summary == {
            "active": 0,
            "reaped_success": 0,
            "reaped_failure": 0,
            "still_running": 0,
            "reaped_untracked": 0,
        }
        # The SELECT ran exactly once with the post-#3329 WHERE shape.
        assert len(select_cur.executed) == 1
        sql, _params = select_cur.executed[0]
        assert "agent_task_arn IS NOT NULL" in sql
        assert "status IN" not in sql


# --------------------------------------------------------------------------
# Issue #3344 — untracked-ARN handling.
#
# After #3329 the SELECT picks up every row with a non-NULL ARN. But ECS
# Fargate retains stopped-task metadata for ~1h; rows older than that
# return NO entry from DescribeTasks (or return one with empty
# lastStatus). Pre-#3344 those rows stayed in the SELECT forever (zero
# forward progress per tick) and eventually wedged the daemon.
#
# The fix: detect ARNs we sent vs ARNs returned with usable metadata,
# and run the same finalize sequence (label strip + merged_at stamp +
# ARN clear) on the difference. Verified by:
# - 5 sent, 2 returned -> 3 untracked finalize sequences fire.
# - 1 returned with empty lastStatus -> treated as untracked.
# - re-running with all rows ARN-cleared -> SELECT empty, zero work.
# --------------------------------------------------------------------------


class TestReapUntrackedArns:
    """Stale-ARN cascade fix (#3344)."""

    def _queue_per_row_finalize_cursors(
        self, conn: _FakeConn, rows_count: int
    ) -> list[_FakeCursor]:
        """Per untracked row, ``_reap_finalize_ecs_success`` issues two
        UPDATEs (merged_at gated, then ARN clear). Queue the cursors so
        the test can assert on each.
        """
        cursors: list[_FakeCursor] = []
        for _ in range(rows_count):
            merged_at_cur = _FakeCursor()
            arn_clear_cur = _FakeCursor()
            conn.queue_cursor(merged_at_cur)
            conn.queue_cursor(arn_clear_cur)
            cursors.extend([merged_at_cur, arn_clear_cur])
        return cursors

    def test_three_of_five_arns_missing_runs_untracked_finalize(
        self, caplog: Any
    ) -> None:
        """SELECT returns 5 ARNs; DescribeTasks returns only 2.

        The 3 missing rows must:
        - emit ``agent_runner_reaped_arn_untracked`` (one per row).
        - run the finalize sequence (label strip + merged_at stamp gated
          by ``pr_number IS NOT NULL`` + ``agent_task_arn = NULL``).
        - drop out of the next tick's SELECT (verified by the ARN-clear
          UPDATE).
        - increment ``reaped_untracked``; not ``still_running``.

        The 2 returned rows follow the regular STOPPED-success path.
        """
        d, conn = _make_daemon()
        d._read_agent_pr_number = lambda agent_id: int(agent_id.split("-")[1]) + 1000  # type: ignore[method-assign]
        d._fetch_pr_status = lambda pr_number: {
            "state": "MERGED",
            "mergedAt": "2026-04-29T12:00:00Z",
        }  # type: ignore[method-assign]
        caplog.set_level(logging.INFO, logger="test.daemon_reap")

        all_arns = [f"arn:aws:ecs:us-west-2:123:task/jm/{i}" for i in range(5)]
        rows = [
            (f"agent-{i}", 1000 + i, all_arns[i], "done", "succeeded") for i in range(5)
        ]
        select_cur = _FakeCursor(rows=rows)
        conn.queue_cursor(select_cur)

        # ECS retention has dropped the first 3 ARNs entirely; only the
        # last 2 come back in the response.
        returned_arns = all_arns[3:]
        missing_arns = all_arns[:3]

        # Per untracked row: 2 cursors (merged_at + ARN clear).
        untracked_cursors = self._queue_per_row_finalize_cursors(
            conn, rows_count=len(missing_arns)
        )

        # The 2 returned rows take the STOPPED-success-already-terminal
        # path. Each needs: status read, finalize merged_at, ARN clear.
        for i in range(3, 5):
            status_cur = _FakeCursor()
            status_cur.fetchone_queue = [("succeeded", "done")]
            conn.queue_cursor(status_cur)
            finalize_cur = _FakeCursor()
            conn.queue_cursor(finalize_cur)
            arn_clear_cur = _FakeCursor()
            conn.queue_cursor(arn_clear_cur)

        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": arn,
                    "lastStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "containers": [{"exitCode": 0}],
                }
                for arn in returned_arns
            ]
        }
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_gh_issue_remove_labels") as remove_labels_mock,
        ):
            summary = d._reap_completed_agent_tasks()

        assert summary["active"] == 5
        assert summary["reaped_untracked"] == 3
        # The 2 returned rows ran the success-already-terminal path.
        assert summary["reaped_success"] == 2
        assert summary["reaped_failure"] == 0
        assert summary["still_running"] == 0

        # Three untracked-ARN events, one per missing row.
        events = [getattr(r, "event", None) for r in caplog.records]
        untracked_events = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "agent_runner_reaped_arn_untracked"
        ]
        assert len(untracked_events) == 3
        # Each event names the untracked ARN + agent_id + issue_number.
        evt_arns = {getattr(r, "task_arn", None) for r in untracked_events}
        assert evt_arns == set(missing_arns)
        evt_agents = {getattr(r, "agent_id", None) for r in untracked_events}
        assert evt_agents == {f"agent-{i}" for i in range(3)}
        evt_issues = {getattr(r, "issue_number", None) for r in untracked_events}
        assert evt_issues == {1000, 1001, 1002}
        # Reason indicates the missing-from-response cause.
        assert all(
            getattr(r, "reason", None) == "not_in_describe_tasks_response"
            for r in untracked_events
        )

        # Label strip called for the 3 untracked + 2 returned = 5 total.
        # Each call uses STATUS_IN_PROGRESS_LABEL.
        assert remove_labels_mock.call_count == 5
        all_label_args = [c.args[1] for c in remove_labels_mock.call_args_list]
        assert all(args == [daemon.STATUS_IN_PROGRESS_LABEL] for args in all_label_args)

        # All three untracked rows issued a merged_at UPDATE (gated by
        # ``pr_number IS NOT NULL`` so it's a no-op when no PR exists)
        # AND an ARN-clear UPDATE. The cursor ordering interleaves
        # (merged_at, arn_clear, merged_at, arn_clear, merged_at,
        # arn_clear) per row.
        for i in range(3):
            merged_cur = untracked_cursors[i * 2]
            arn_cur = untracked_cursors[i * 2 + 1]
            merged_sqls = [s for s, _ in merged_cur.executed]
            assert any("merged_at" in s for s in merged_sqls), merged_sqls
            assert any("merged_at IS NULL" in s for s in merged_sqls)
            assert any("pr_number IS NOT NULL" in s for s in merged_sqls)
            arn_sqls = [s for s, _ in arn_cur.executed]
            assert any("SET agent_task_arn = NULL" in s for s in arn_sqls), arn_sqls

        # ``still_running`` did not absorb any of the untracked rows —
        # this is the regression bar pre-#3344 was missing.
        assert "agent_runner_reaped_arn_untracked" in events

    def test_empty_last_status_treated_as_untracked(self, caplog: Any) -> None:
        """Fargate occasionally returns an entry with empty lastStatus
        — it counts as no usable metadata, same as missing entirely.

        SELECT returns 1 ARN; DescribeTasks returns it but with
        ``lastStatus`` empty. The row must be reaped via the untracked
        path (label strip + merged_at + ARN clear), NOT carried as
        ``still_running``.
        """
        d, conn = _make_daemon()
        d._read_agent_pr_number = lambda agent_id: 4242  # type: ignore[method-assign]
        d._fetch_pr_status = lambda pr_number: {
            "state": "MERGED",
            "mergedAt": "2026-04-29T12:00:00Z",
        }  # type: ignore[method-assign]
        caplog.set_level(logging.INFO, logger="test.daemon_reap")

        arn = "arn:aws:ecs:us-west-2:123:task/jm/empty-status"
        select_cur = _FakeCursor(rows=[("agent-empty", 4242, arn, "done", "succeeded")])
        conn.queue_cursor(select_cur)
        # Per-untracked finalize cursors.
        merged_cur = _FakeCursor()
        arn_clear_cur = _FakeCursor()
        conn.queue_cursor(merged_cur)
        conn.queue_cursor(arn_clear_cur)

        fake_client = MagicMock()
        fake_client.describe_tasks.return_value = {
            "tasks": [
                {
                    # Returned, but ``lastStatus`` empty — Fargate stub
                    # response shape that conveys no useful state.
                    "taskArn": arn,
                    "lastStatus": "",
                }
            ]
        }
        with (
            patch.object(d, "_make_ecs_client", return_value=fake_client),
            patch.object(d, "_gh_issue_remove_labels") as remove_labels_mock,
            patch.object(d, "_handle_agent_failure") as fail_mock,
            patch.object(d, "_mark_agent_terminal") as mark_mock,
        ):
            summary = d._reap_completed_agent_tasks()

        assert summary["reaped_untracked"] == 1
        assert summary["still_running"] == 0
        assert summary["reaped_success"] == 0
        assert summary["reaped_failure"] == 0
        # Untracked path does not route via _handle_agent_failure or
        # _mark_agent_terminal — it relies on the agent-runner having
        # already written its terminal row (or accepts the row as gone).
        fail_mock.assert_not_called()
        mark_mock.assert_not_called()
        # Label stripped, merged_at gated, ARN cleared.
        remove_labels_mock.assert_called_once_with(
            4242, [daemon.STATUS_IN_PROGRESS_LABEL]
        )
        merged_sqls = [s for s, _ in merged_cur.executed]
        assert any("merged_at" in s for s in merged_sqls)
        arn_sqls = [s for s, _ in arn_clear_cur.executed]
        assert any("SET agent_task_arn = NULL" in s for s in arn_sqls)
        # Event reason should indicate empty lastStatus.
        evts = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "agent_runner_reaped_arn_untracked"
        ]
        assert len(evts) == 1
        assert getattr(evts[0], "reason", None) == "empty_last_status"

    def test_idempotence_after_arn_clear_select_empty(self) -> None:
        """After a tick clears all 5 ARNs via the untracked path, the
        next reaper tick's SELECT (gated by ``agent_task_arn IS NOT
        NULL``) returns zero rows — no ECS calls, no UPDATEs, no log
        spam.

        This pins the cascade exit: an empty result set is the desired
        steady state once the stale-ARN backlog drains.
        """
        d, conn = _make_daemon()
        # Empty result set models the post-clear DB state — every row
        # the previous tick finalized via untracked has ``agent_task_arn
        # = NULL`` and is excluded from the WHERE clause.
        select_cur = _FakeCursor(rows=[])
        conn.queue_cursor(select_cur)

        with (
            patch.object(d, "_make_ecs_client") as mock_make_client,
            patch.object(d, "_gh_issue_remove_labels") as remove_labels_mock,
            patch.object(d, "_mark_agent_terminal") as mark_mock,
            patch.object(d, "_handle_agent_failure") as fail_mock,
        ):
            summary = d._reap_completed_agent_tasks()

        # Zero-work early return — same shape as TestReapArnClearIdempotence.
        mock_make_client.assert_not_called()
        remove_labels_mock.assert_not_called()
        mark_mock.assert_not_called()
        fail_mock.assert_not_called()
        assert summary == {
            "active": 0,
            "reaped_success": 0,
            "reaped_failure": 0,
            "still_running": 0,
            "reaped_untracked": 0,
        }
