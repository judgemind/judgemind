"""Unit tests for the extracted _watch_in_flight branch helpers (issue #3943).

Exercises ``_resolve_stopped_task`` and ``_maybe_reap_silent_hang`` in
isolation so each helper can be tested without the full DescribeTasks shim
that the end-to-end ``_watch_in_flight`` integration tests in
``test_launcher.py`` require.

Each test constructs a :class:`Launcher` with minimal stubs and calls the
helper directly, asserting the returned transition dict and side-effect
calls. The helpers share the same psycopg shim as the other test modules.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

# Inject a fake ``psycopg.errors`` module so the launcher's lazy import
# finds something callable in environments where the real wheel is not
# installed. Matches the shim in test_launcher.py and test_diagnoser_invocation.py.
if "psycopg" not in sys.modules:
    fake_psycopg = types.ModuleType("psycopg")
    fake_errors = types.ModuleType("psycopg.errors")

    class _FakeUniqueViolation(Exception):
        pass

    fake_errors.UniqueViolation = _FakeUniqueViolation
    fake_psycopg.errors = fake_errors
    sys.modules["psycopg"] = fake_psycopg
    sys.modules["psycopg.errors"] = fake_errors


from dispatcher_v3.launcher import (  # noqa: E402 — must follow sys.modules patch
    EXIT_REASON_SILENT_HANG,
    Launcher,
)


# ---------------------------------------------------------------------------
# Minimal fakes — enough to exercise the helpers without a full DB loop
# ---------------------------------------------------------------------------


class FakeCursor:
    """Minimal cursor stub."""

    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.conn.executed.append((sql, params or ()))
        for predicate, handler in self.conn.handlers:
            if predicate in sql:
                handler(self, sql, params or ())
                return
        self._next_fetchone = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return getattr(self, "_next_fetchone", None)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return getattr(self, "_next_fetchall", []) or []


class FakeConn:
    """Minimal connection stub that records executed SQL."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.handlers: list[tuple[str, Any]] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def install_handler(self, predicate: str, handler: Any) -> None:
        self.handlers.append((predicate, handler))


def make_launcher(
    conn: FakeConn | None = None,
    ecs_client: MagicMock | None = None,
    cloudwatch_logs_client: MagicMock | None = None,
    task_runner_log_group: str = "",
    diagnoser_task_definition: str = "judgemind-dispatcher-v3-diagnoser",
) -> Launcher:
    """Construct a Launcher wired for helper-level unit tests."""
    return Launcher(
        run_id="run-test-uuid",
        github_repo="judgemind/judgemind",
        ecs_cluster_arn="arn:aws:ecs:us-west-2:0:cluster/jm",
        task_runner_task_definition="judgemind-task-runner:7",
        diagnoser_task_definition=diagnoser_task_definition,
        agent_runner_subnet_ids=["subnet-a"],
        agent_runner_security_group_id="sg-aaa",
        sessions_bucket="judgemind-sessions-dev",
        task_runner_log_group=task_runner_log_group,
        conn=conn,
        ecs_client=ecs_client,
        cloudwatch_logs_client=cloudwatch_logs_client,
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )


# ---------------------------------------------------------------------------
# _resolve_stopped_task
# ---------------------------------------------------------------------------


def test_resolve_stopped_task_success_no_diagnoser() -> None:
    """exit_code=0 → status='succeeded', no diagnoser invoked, no diagnoser_arn."""
    conn = FakeConn()
    ecs = MagicMock()
    launcher = make_launcher(conn=conn, ecs_client=ecs)

    desc = {
        "taskArn": "arn:task/1",
        "lastStatus": "STOPPED",
        "containers": [{"exitCode": 0}],
        "stoppedReason": "Essential container exited",
    }
    result = launcher._resolve_stopped_task(
        agent_id="ag-1",
        task_arn="arn:task/1",
        issue_number=42,
        desc=desc,
    )

    assert result["agent_id"] == "ag-1"
    assert result["status"] == "succeeded"
    assert result["exit_code"] == 0
    assert "diagnoser_arn" not in result
    # No ECS RunTask (diagnoser launch) for a success transition.
    ecs.run_task.assert_not_called()
    # DB UPDATE for status='succeeded' fired.
    assert any(
        sql.startswith("UPDATE dispatcher.agents")
        and "SET status = %s" in sql
        and params[0] == "succeeded"
        for sql, params in conn.executed
    )


def test_resolve_stopped_task_failure_invokes_diagnoser() -> None:
    """exit_code=1 → status='failed', _launch_diagnoser called, diagnoser_arn populated."""
    conn = FakeConn()
    # _launch_diagnoser's cap-check SELECT returns no prior diagnoser.
    conn.install_handler(
        "SELECT diagnoser_arn FROM dispatcher.agents",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (None,)),
    )
    ecs = MagicMock()
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:task/diag-1"}],
        "failures": [],
    }
    launcher = make_launcher(conn=conn, ecs_client=ecs)

    desc = {
        "taskArn": "arn:task/1",
        "lastStatus": "STOPPED",
        "containers": [{"exitCode": 1}],
        "stoppedReason": "Error: something went wrong",
    }
    result = launcher._resolve_stopped_task(
        agent_id="ag-1",
        task_arn="arn:task/1",
        issue_number=42,
        desc=desc,
    )

    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert result["diagnoser_arn"] == "arn:task/diag-1"
    # _launch_diagnoser called exactly once.
    ecs.run_task.assert_called_once()
    kwargs = ecs.run_task.call_args.kwargs
    assert kwargs["taskDefinition"] == "judgemind-dispatcher-v3-diagnoser"
    env_pairs = kwargs["overrides"]["containerOverrides"][0]["environment"]
    env = {p["name"]: p["value"] for p in env_pairs}
    assert env == {"AGENT_ID": "ag-1"}


def test_resolve_stopped_task_failure_no_diagnoser_arn_when_disabled() -> None:
    """_launch_diagnoser returns None (disabled) → no diagnoser_arn key in result."""
    conn = FakeConn()
    ecs = MagicMock()
    # Empty diagnoser task-def disables the launch.
    launcher = make_launcher(conn=conn, ecs_client=ecs, diagnoser_task_definition="")

    desc = {
        "taskArn": "arn:task/1",
        "lastStatus": "STOPPED",
        "containers": [{"exitCode": 7}],
        "stoppedReason": "OOM",
    }
    result = launcher._resolve_stopped_task(
        agent_id="ag-1",
        task_arn="arn:task/1",
        issue_number=42,
        desc=desc,
    )

    assert result["status"] == "failed"
    assert "diagnoser_arn" not in result
    ecs.run_task.assert_not_called()


# ---------------------------------------------------------------------------
# _maybe_reap_silent_hang
# ---------------------------------------------------------------------------


def test_maybe_reap_silent_hang_returns_none_without_log_group() -> None:
    """Empty task_runner_log_group → detector OFF, returns None immediately."""
    conn = FakeConn()
    cw = MagicMock()
    # task_runner_log_group="" (the default) disables the detector.
    launcher = make_launcher(
        conn=conn, cloudwatch_logs_client=cw, task_runner_log_group=""
    )

    result = launcher._maybe_reap_silent_hang(
        agent_id="ag-1",
        task_arn="arn:task/1",
        issue_number=42,
        threshold_seconds=1800.0,
    )

    assert result is None
    cw.describe_log_streams.assert_not_called()


def test_maybe_reap_silent_hang_returns_none_when_not_stale() -> None:
    """A fresh log stream is not stale — returns None, no stop/mark/diagnoser."""
    import time as _time

    conn = FakeConn()
    ecs = MagicMock()
    cw = MagicMock()
    fresh_ms = int((_time.time() - 5) * 1000)  # 5 seconds ago
    cw.describe_log_streams.return_value = {
        "logStreams": [
            {
                "logStreamName": "task-runner/task-runner/abc123",
                "lastEventTimestamp": fresh_ms,
            }
        ]
    }
    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        cloudwatch_logs_client=cw,
        task_runner_log_group="/ecs/judgemind-task-runner-dev",
    )

    result = launcher._maybe_reap_silent_hang(
        agent_id="ag-1",
        task_arn="arn:aws:ecs:us-west-2:0:task/jm/abc123",
        issue_number=42,
        threshold_seconds=1800.0,  # 30 min — stream is only 5s old
    )

    assert result is None
    ecs.stop_task.assert_not_called()
    assert not any(
        sql.startswith("UPDATE dispatcher.agents") and "SET status = %s" in sql
        for sql, _ in conn.executed
    )


def test_maybe_reap_silent_hang_stale_stops_marks_logs_and_diagnoses() -> None:
    """Stale log stream → StopTask, mark failed, emit warning, return transition."""
    import time as _time

    conn = FakeConn()
    # _launch_diagnoser cap-check sees no prior diagnoser.
    conn.install_handler(
        "SELECT diagnoser_arn FROM dispatcher.agents",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (None,)),
    )
    ecs = MagicMock()
    ecs.stop_task.return_value = {}
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:task/diag-1"}],
        "failures": [],
    }
    cw = MagicMock()
    stale_ms = int((_time.time() - (45 * 60)) * 1000)  # 45 min ago
    cw.describe_log_streams.return_value = {
        "logStreams": [
            {
                "logStreamName": "task-runner/task-runner/abc123",
                "lastEventTimestamp": stale_ms,
            }
        ]
    }
    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        cloudwatch_logs_client=cw,
        task_runner_log_group="/ecs/judgemind-task-runner-dev",
    )

    result = launcher._maybe_reap_silent_hang(
        agent_id="ag-1",
        task_arn="arn:aws:ecs:us-west-2:0:task/jm/abc123",
        issue_number=42,
        threshold_seconds=1800.0,  # 30 min — stream is 45 min old → stale
    )

    # Returned transition reflects the reap.
    assert result is not None
    assert result["agent_id"] == "ag-1"
    assert result["status"] == "failed"
    assert result["exit_code"] is None
    assert result["exit_reason"] == EXIT_REASON_SILENT_HANG
    assert result["diagnoser_arn"] == "arn:task/diag-1"

    # ecs:StopTask called with the right ARN.
    assert ecs.stop_task.call_count == 1
    assert (
        ecs.stop_task.call_args.kwargs["task"]
        == "arn:aws:ecs:us-west-2:0:task/jm/abc123"
    )

    # DB UPDATE marks agent failed with exit_reason='silent_hang'.
    matching = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("UPDATE dispatcher.agents") and "SET status = %s" in sql
    ]
    assert matching
    _, params = matching[-1]
    assert params[0] == "failed"
    assert params[2] == EXIT_REASON_SILENT_HANG

    # Diagnoser launched once.
    ecs.run_task.assert_called_once()
