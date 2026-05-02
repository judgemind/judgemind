"""Unit tests for ``dispatcher_v3.launcher``.

The launcher's external surface — DB, ECS, gh CLI — is mocked. Each
test drives one tick (or one helper) against deterministic fakes and
asserts the side effects (rows inserted, labels flipped, RunTask
called) match the v3 spec §4.1 sequence.

Key invariants pinned by the suite:

- **Atomic claim ordering matches v2.** The DB INSERT runs *before*
  any label edit and *before* ``ecs:RunTask``. v2's order is:
  INSERT row → add ``status/in-progress`` → remove ``agent/ready`` →
  ``ecs:RunTask`` → UPDATE row with ``task_arn``.
  ``test_claim_happy_path_records_call_order`` asserts the order
  exactly so a future "let's add the label first" refactor fails
  loudly. Issue #3880 calls this out as a security gate during v2/v3
  cohabitation — getting it wrong races against v2.

- **Race loss is silent and clean.** A concurrent INSERT raising
  :class:`psycopg.errors.UniqueViolation` (the partial UNIQUE INDEX
  from migration 25 firing) returns False with no label edits and no
  RunTask call. ``test_claim_race_uniqueviolation_abandons_cleanly``
  pins this.

- **Budget exhaustion is per-issue.** The query counts v3 rows for
  the issue without filtering by ``parent_run_id`` so a launcher
  restart does not reset the budget. Acceptance criterion: "Claim
  budget query is scoped (no ``parent_run_id`` filter — the budget is
  per-issue across all attempts, intentional)."
  ``test_budget_exhaustion_skips_and_marks_needs_human`` pins this.

- **Partial-claim recovery.** Rows with ``current_milestone='claiming'
  AND task_arn IS NULL AND age > 5min`` are marked ``failed`` with
  ``exit_reason='claim_abandoned'`` so the issue can be re-claimed.
  ``test_recover_partial_claims_marks_old_rows_failed`` pins this.

- **Watch happy paths.** STOPPED-exit-0 → ``succeeded``;
  STOPPED-non-zero → ``failed`` with ``stoppedReason`` captured.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

# Inject a fake ``psycopg.errors`` module up front so the launcher's
# lazy ``import psycopg`` finds something callable in environments
# where the real wheel is not installed (CI test runner per
# ``.github/workflows/ci.yml`` line 619).
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
    DEFAULT_CLAIM_ATTEMPTS_MAX,
    DEFAULT_CONCURRENCY_CAP_V3,
    DEFAULT_DIAGNOSER_WALL_CLOCK_SECONDS,
    DEFAULT_SCHEDULED_SKILL_WALL_CLOCK_SECONDS,
    DEFAULT_SILENT_HANG_MINUTES,
    DEFAULT_TASK_RUNNER_LOG_STREAM_PREFIX,
    DEFAULT_TASK_RUNNER_WALL_CLOCK_SECONDS,
    EXIT_REASON_SILENT_HANG,
    EXIT_REASON_WALL_CLOCK_EXCEEDED,
    LABEL_AGENT_READY,
    LABEL_DISPATCHER_V2_ONLY,
    LABEL_STATUS_IN_PROGRESS,
    LABEL_STATUS_NEEDS_HUMAN,
    PARTIAL_CLAIM_RECOVERY_AGE_SECONDS,
    Launcher,
    _build_arg_parser,
    _parse_int_env,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCursor:
    """Tiny in-memory cursor that records every executed SQL."""

    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.conn.executed.append((sql, params or ()))
        # Run the response handler the test installed for this query
        # shape (the conn dispatches by substring match — simpler than
        # writing a SQL parser for tests).
        for predicate, handler in self.conn.handlers:
            if predicate in sql:
                handler(self, sql, params or ())
                return
        # Default: leave fetch results empty.
        self._next_fetchone = None
        self._next_fetchall: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def fetchone(self) -> tuple[Any, ...] | None:
        return getattr(self, "_next_fetchone", None)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return getattr(self, "_next_fetchall", []) or []


class FakeConn:
    """Fake psycopg connection that records executed SQL + commits."""

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
        """Register a SQL-substring → response-handler binding.

        The handler signature is ``(cur, sql, params)``; it sets
        ``cur._next_fetchone``, ``cur._next_fetchall``, and/or
        ``cur.rowcount`` to drive subsequent fetch calls.
        """
        self.handlers.append((predicate, handler))


def make_launcher(
    conn: FakeConn | None = None,
    ecs_client: MagicMock | None = None,
    cloudwatch_logs_client: MagicMock | None = None,
    subprocess_runner: Any = None,
    trust_checker: Any = None,
    runner_name: str = "claude",
    task_runner_log_group: str = "",
    task_runner_log_stream_prefix: str = DEFAULT_TASK_RUNNER_LOG_STREAM_PREFIX,
    diagnoser_task_definition: str = "judgemind-dispatcher-v3-diagnoser",
    task_runner_wall_clock_seconds: int = DEFAULT_TASK_RUNNER_WALL_CLOCK_SECONDS,
    diagnoser_wall_clock_seconds: int = DEFAULT_DIAGNOSER_WALL_CLOCK_SECONDS,
    scheduled_skill_wall_clock_seconds: int = DEFAULT_SCHEDULED_SKILL_WALL_CLOCK_SECONDS,
) -> Launcher:
    """Construct a :class:`Launcher` with sane test defaults.

    The silent-hang detector is OFF by default (``task_runner_log_group=""``)
    so existing watch tests don't have to mock CloudWatch. Tests that
    exercise the detector pass an explicit ``task_runner_log_group``.

    Wall-clock caps default to the production values; tests for the
    wall-clock path pass small caps to drive a deterministic reap.
    """
    return Launcher(
        run_id="run-test-uuid",
        github_repo="judgemind/judgemind",
        ecs_cluster_arn="arn:aws:ecs:us-west-2:0:cluster/jm",
        task_runner_task_definition="judgemind-task-runner:7",
        diagnoser_task_definition=diagnoser_task_definition,
        agent_runner_subnet_ids=["subnet-a", "subnet-b"],
        agent_runner_security_group_id="sg-aaa",
        sessions_bucket="judgemind-sessions-dev",
        runner_name=runner_name,
        task_runner_log_group=task_runner_log_group,
        task_runner_log_stream_prefix=task_runner_log_stream_prefix,
        task_runner_wall_clock_seconds=task_runner_wall_clock_seconds,
        diagnoser_wall_clock_seconds=diagnoser_wall_clock_seconds,
        scheduled_skill_wall_clock_seconds=scheduled_skill_wall_clock_seconds,
        conn=conn,
        ecs_client=ecs_client,
        cloudwatch_logs_client=cloudwatch_logs_client,
        subprocess_runner=subprocess_runner,
        trust_checker=trust_checker,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_help_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    """``python -m dispatcher_v3.launcher --help`` prints usage.

    Pinned because the issue's first acceptance criterion ("``python -m
    dispatcher_v3.launcher --help`` prints usage") is the only thing
    sanity-testable without importing AWS or psycopg.
    """
    parser = _build_arg_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "dispatcher_v3.launcher" in out
    assert "--tick-interval" in out


# ---------------------------------------------------------------------------
# Atomic claim ordering — happy path
# ---------------------------------------------------------------------------


def test_claim_happy_path_records_call_order() -> None:
    """Atomic claim sequence matches v2's order exactly.

    Order asserted: DB INSERT → add ``status/in-progress`` → remove
    ``agent/ready`` → ``ecs:RunTask`` → UPDATE row with ``task_arn``.
    Critical for v2/v3 cohabitation correctness — see #3880 and the
    spec §4.1 atomic-claim block.
    """
    conn = FakeConn()
    # No prior agents for this issue (count = 0 < budget).
    conn.install_handler(
        "FROM dispatcher.config WHERE key = %s",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", ("1",)),
    )
    conn.install_handler(
        "COUNT(*) FROM dispatcher.agents",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (0,)),
    )

    ecs = MagicMock()
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:0:task/abc"}],
        "failures": [],
    }

    # Subprocess runner records every gh CLI call so we can assert
    # label-flip ordering against ECS RunTask.
    invocations: list[list[str]] = []

    def fake_subprocess(cmd: list[str], **kwargs: Any) -> Any:
        invocations.append(cmd)
        if cmd[1:3] == ["issue", "list"]:
            return MagicMock(
                returncode=0,
                stdout=(
                    '[{"number": 9001, "title": "Test issue", '
                    '"labels": [{"name": "agent/ready"}], '
                    '"createdAt": "2026-05-01T00:00:00Z"}]'
                ),
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        subprocess_runner=fake_subprocess,
        trust_checker=lambda n: True,
    )
    claims, skipped = launcher._claim_if_cap_allows()

    assert len(claims) == 1
    assert claims[0]["number"] == 9001
    assert claims[0]["task_arn"] == "arn:aws:ecs:us-west-2:0:task/abc"
    assert skipped == []

    # Recover the SQL trace and the gh CLI trace and assert ordering:
    # the INSERT must precede the label edits AND the ECS RunTask call.
    insert_idx = next(
        i
        for i, (sql, _) in enumerate(conn.executed)
        if sql.startswith("INSERT INTO dispatcher.agents")
    )
    update_idx = next(
        i
        for i, (sql, _) in enumerate(conn.executed)
        if sql.startswith("UPDATE dispatcher.agents") and "task_arn" in sql
    )
    assert insert_idx < update_idx, "DB INSERT must precede the task_arn UPDATE"

    add_label_idx = next(
        i
        for i, cmd in enumerate(invocations)
        if cmd[1:4] == ["issue", "edit", "9001"] and "--add-label" in cmd
    )
    remove_label_idx = next(
        i
        for i, cmd in enumerate(invocations)
        if cmd[1:4] == ["issue", "edit", "9001"] and "--remove-label" in cmd
    )
    assert add_label_idx < remove_label_idx, (
        "add status/in-progress must precede remove agent/ready"
    )

    # ECS RunTask called exactly once with the right env-var overrides.
    assert ecs.run_task.call_count == 1
    kwargs = ecs.run_task.call_args.kwargs
    env_pairs = kwargs["overrides"]["containerOverrides"][0]["environment"]
    env_by_name = {p["name"]: p["value"] for p in env_pairs}
    assert env_by_name["TASK_ISSUE_NUMBER"] == "9001"
    assert env_by_name["RUNNER"] == "claude"
    assert env_by_name["SESSIONS_BUCKET"] == "judgemind-sessions-dev"
    assert env_by_name["AGENT_ID"]  # generated UUID, non-empty
    # And the labels touched are the right ones.
    assert any(
        cmd[1:4] == ["issue", "edit", "9001"]
        and "--add-label" in cmd
        and LABEL_STATUS_IN_PROGRESS in cmd[-1]
        for cmd in invocations
    )
    assert any(
        cmd[1:4] == ["issue", "edit", "9001"]
        and "--remove-label" in cmd
        and LABEL_AGENT_READY in cmd[-1]
        for cmd in invocations
    )


def test_claim_race_uniqueviolation_abandons_cleanly() -> None:
    """A concurrent INSERT race aborts with no labels touched, no RunTask."""
    import psycopg  # noqa: PLC0415 — registered fake

    conn = FakeConn()

    def insert_raises(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        raise psycopg.errors.UniqueViolation("active row already exists")

    conn.install_handler("INSERT INTO dispatcher.agents", insert_raises)

    ecs = MagicMock()
    invocations: list[list[str]] = []

    def fake_subprocess(cmd: list[str], **kwargs: Any) -> Any:
        invocations.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        subprocess_runner=fake_subprocess,
    )
    outcome = launcher._claim_one(agent_id="a-aaaa", issue_number=42)

    assert outcome == {"ok": False, "reason": "claim_lost"}
    # ECS RunTask never called.
    ecs.run_task.assert_not_called()
    # No gh edit calls — the race-loser does not touch labels.
    assert all(cmd[1:3] != ["issue", "edit"] for cmd in invocations)
    # Rollback fired so the connection is left clean.
    assert conn.rollbacks >= 1


# ---------------------------------------------------------------------------
# Watch in-flight tasks
# ---------------------------------------------------------------------------


def test_watch_marks_succeeded_on_stopped_exit_zero() -> None:
    conn = FakeConn()
    conn.install_handler(
        "SELECT agent_id, task_arn, issue_number, started_at FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur,
            "_next_fetchall",
            # Pre-#3940 the row was (agent_id, task_arn, issue_number);
            # post-#3940 the watch query also reads started_at for the
            # wall-clock-cap check. Pass None to skip the check (this
            # test is about STOPPED-exit-0 + RUNNING coexistence, not
            # the wall-clock path).
            [
                ("ag-1", "arn:task/1", 100, None),
                ("ag-2", "arn:task/2", 101, None),
            ],
        ),
    )

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:task/1",
                "lastStatus": "STOPPED",
                "containers": [{"exitCode": 0}],
                "stoppedReason": "Essential container exited",
            },
            {
                "taskArn": "arn:task/2",
                "lastStatus": "RUNNING",
                "containers": [{}],
            },
        ]
    }

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    watched, transitions = launcher._watch_in_flight()
    assert watched == 2
    assert len(transitions) == 1
    assert transitions[0]["agent_id"] == "ag-1"
    assert transitions[0]["status"] == "succeeded"
    assert transitions[0]["exit_code"] == 0
    # The UPDATE for ag-1 fired with status='succeeded'.
    assert any(
        sql.startswith("UPDATE dispatcher.agents")
        and "SET status = %s" in sql
        and params[0] == "succeeded"
        and params[3] == "ag-1"
        for sql, params in conn.executed
    )


def test_watch_marks_failed_on_stopped_nonzero_with_reason() -> None:
    conn = FakeConn()
    conn.install_handler(
        "SELECT agent_id, task_arn, issue_number, started_at FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur,
            "_next_fetchall",
            # 4th column is started_at; None skips the wall-clock check.
            [("ag-1", "arn:task/1", 100, None)],
        ),
    )

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:task/1",
                "lastStatus": "STOPPED",
                "containers": [{"exitCode": 7}],
                "stoppedReason": "OutOfMemoryError: Container killed due to memory usage",
            },
        ]
    }

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()
    assert len(transitions) == 1
    assert transitions[0]["status"] == "failed"
    assert transitions[0]["exit_code"] == 7
    assert "OutOfMemoryError" in transitions[0]["exit_reason"]


def test_watch_handles_missing_exit_code_as_failure() -> None:
    """A STOPPED task without an exitCode resolves to failed (exit_code=-1)."""
    conn = FakeConn()
    conn.install_handler(
        "SELECT agent_id, task_arn, issue_number, started_at FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur,
            "_next_fetchall",
            # 4th column is started_at; None skips the wall-clock check.
            [("ag-1", "arn:task/1", 100, None)],
        ),
    )
    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:task/1",
                "lastStatus": "STOPPED",
                "containers": [{}],
                "stoppedReason": "Task stopped before container exit",
            }
        ]
    }
    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()
    assert len(transitions) == 1
    assert transitions[0]["status"] == "failed"
    assert transitions[0]["exit_code"] == -1


# ---------------------------------------------------------------------------
# Silent-hang detector (issue #3881)
# ---------------------------------------------------------------------------


def _install_running_agent_row(
    conn: FakeConn,
    *,
    agent_id: str = "ag-1",
    task_arn: str = "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
    issue_number: int = 9001,
    started_at: Any = None,
) -> None:
    """Install handler returning one RUNNING agent row for the watch query.

    Post-#3940 the watch query includes ``started_at`` for the
    wall-clock-cap check. ``started_at=None`` (the default) skips the
    wall-clock check; tests that exercise the wall-clock path pass an
    explicit UTC ``datetime``.
    """
    conn.install_handler(
        "SELECT agent_id, task_arn, issue_number, started_at FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur,
            "_next_fetchall",
            [(agent_id, task_arn, issue_number, started_at)],
        ),
    )


def _install_silent_hang_config(conn: FakeConn, *, minutes: int | None) -> None:
    """Install a config-row handler that returns ``minutes`` for ``silent_hang_minutes``.

    ``minutes=None`` simulates a missing config row (default fallback).
    """

    def handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        key = params[0] if params else ""
        if key == "silent_hang_minutes":
            cur._next_fetchone = None if minutes is None else (str(minutes),)
        else:
            cur._next_fetchone = None

    conn.install_handler("FROM dispatcher.config WHERE key = %s", handler)


def test_silent_hang_reaps_stale_running_task() -> None:
    """RUNNING task whose log stream is older than threshold gets reaped.

    Asserts: (1) ``ecs:StopTask`` was called with the right ARN; (2)
    the agent row's UPDATE used ``status='failed'`` and
    ``exit_reason='silent_hang'``; (3) the transition the watch loop
    returns reflects the reap.
    """
    import time as _time

    conn = FakeConn()
    _install_running_agent_row(conn)
    _install_silent_hang_config(conn, minutes=None)  # fall back to default 30

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }

    # Stream's last event is 45min ago (> 30min default threshold).
    stale_ms = int((_time.time() - (45 * 60)) * 1000)
    cw = MagicMock()
    cw.describe_log_streams.return_value = {
        "logStreams": [
            {
                "logStreamName": "task-runner/task-runner/abc123def456",
                "lastEventTimestamp": stale_ms,
            }
        ]
    }

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        cloudwatch_logs_client=cw,
        task_runner_log_group="/ecs/judgemind-task-runner-dev",
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    watched, transitions = launcher._watch_in_flight()

    assert watched == 1
    # The DescribeLogStreams call used the right group + stream name.
    assert cw.describe_log_streams.call_count == 1
    call_kwargs = cw.describe_log_streams.call_args.kwargs
    assert call_kwargs["logGroupName"] == "/ecs/judgemind-task-runner-dev"
    assert call_kwargs["logStreamNamePrefix"] == (
        "task-runner/task-runner/abc123def456"
    )
    # ecs:StopTask called with the right ARN and a `silent_hang:` reason.
    assert ecs.stop_task.call_count == 1
    stop_kwargs = ecs.stop_task.call_args.kwargs
    assert stop_kwargs["task"] == "arn:aws:ecs:us-west-2:0:task/jm/abc123def456"
    assert stop_kwargs["reason"].startswith("silent_hang:")
    # Agent row UPDATE marks failed with exit_reason='silent_hang'.
    matching = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("UPDATE dispatcher.agents") and "SET status = %s" in sql
    ]
    assert matching, "Expected an UPDATE to dispatcher.agents on silent_hang"
    _, params = matching[-1]
    assert params[0] == "failed"
    assert params[2] == EXIT_REASON_SILENT_HANG
    # Transition surfaced to the caller.
    assert len(transitions) == 1
    assert transitions[0]["status"] == "failed"
    assert transitions[0]["exit_reason"] == EXIT_REASON_SILENT_HANG


def test_silent_hang_skips_when_log_stream_fresh() -> None:
    """RUNNING task with a recent log event is left alone (no reap)."""
    import time as _time

    conn = FakeConn()
    _install_running_agent_row(conn)
    _install_silent_hang_config(conn, minutes=None)

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }

    fresh_ms = int((_time.time() - 5) * 1000)  # 5s ago
    cw = MagicMock()
    cw.describe_log_streams.return_value = {
        "logStreams": [
            {
                "logStreamName": "task-runner/task-runner/abc123def456",
                "lastEventTimestamp": fresh_ms,
            }
        ]
    }

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        cloudwatch_logs_client=cw,
        task_runner_log_group="/ecs/judgemind-task-runner-dev",
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()

    assert transitions == []
    ecs.stop_task.assert_not_called()
    # No UPDATE flipping the agent to failed.
    assert not any(
        sql.startswith("UPDATE dispatcher.agents") and "SET status = %s" in sql
        for sql, _ in conn.executed
    )


def test_silent_hang_skips_when_log_stream_not_yet_found() -> None:
    """A just-launched task (no log stream yet) is NOT reaped.

    The awslogs driver creates the stream on the first event, which
    can lag the ECS RUNNING transition by several seconds. Reaping in
    that window would kill every freshly-launched agent.
    """
    conn = FakeConn()
    _install_running_agent_row(conn)
    _install_silent_hang_config(conn, minutes=None)

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }

    cw = MagicMock()
    # Empty logStreams = stream not yet created.
    cw.describe_log_streams.return_value = {"logStreams": []}

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        cloudwatch_logs_client=cw,
        task_runner_log_group="/ecs/judgemind-task-runner-dev",
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()

    assert transitions == []
    ecs.stop_task.assert_not_called()
    assert not any(
        sql.startswith("UPDATE dispatcher.agents") and "SET status = %s" in sql
        for sql, _ in conn.executed
    )


def test_silent_hang_skips_when_describe_streams_raises() -> None:
    """A CloudWatch API error is logged but does not reap the agent.

    CW transient errors (rate limits, IAM, network) must not flip
    healthy agents to failed. Fail-closed on the side of NOT reaping.
    """
    conn = FakeConn()
    _install_running_agent_row(conn)
    _install_silent_hang_config(conn, minutes=None)

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }

    cw = MagicMock()
    cw.describe_log_streams.side_effect = RuntimeError("Throttled")

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        cloudwatch_logs_client=cw,
        task_runner_log_group="/ecs/judgemind-task-runner-dev",
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()

    assert transitions == []
    ecs.stop_task.assert_not_called()


def test_silent_hang_skipped_entirely_when_log_group_unset() -> None:
    """Empty TASK_RUNNER_LOG_GROUP → detector OFF (no CW calls at all).

    The graceful-degradation path: F2 has not yet wired the awslogs
    group, so the launcher must not crash trying to query a
    nonexistent group. No CW client is even constructed.
    """
    conn = FakeConn()
    _install_running_agent_row(conn)

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }

    cw = MagicMock()

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        cloudwatch_logs_client=cw,
        # task_runner_log_group="" by default in make_launcher.
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()

    assert transitions == []
    cw.describe_log_streams.assert_not_called()
    ecs.stop_task.assert_not_called()


def test_silent_hang_threshold_reads_from_config() -> None:
    """``dispatcher.config.silent_hang_minutes`` overrides the default.

    With a 15-minute override, a 20-minute-old log stream is reaped
    (it would not be reaped under the default 30-minute threshold).
    Pins the AC: "Threshold reads from dispatcher.config.silent_hang_minutes
    with default 30. Verify: test with config=15 and confirm threshold
    takes effect."
    """
    import time as _time

    conn = FakeConn()
    _install_running_agent_row(conn)
    _install_silent_hang_config(conn, minutes=15)

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }

    # 20min stale: under the default 30min threshold this would NOT
    # reap, but with the config=15min override it SHOULD reap.
    stale_ms = int((_time.time() - (20 * 60)) * 1000)
    cw = MagicMock()
    cw.describe_log_streams.return_value = {
        "logStreams": [
            {
                "logStreamName": "task-runner/task-runner/abc123def456",
                "lastEventTimestamp": stale_ms,
            }
        ]
    }

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        cloudwatch_logs_client=cw,
        task_runner_log_group="/ecs/judgemind-task-runner-dev",
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()

    assert len(transitions) == 1
    assert transitions[0]["exit_reason"] == EXIT_REASON_SILENT_HANG
    assert ecs.stop_task.call_count == 1


def test_read_silent_hang_minutes_default_when_missing() -> None:
    """Missing config row → DEFAULT_SILENT_HANG_MINUTES (30)."""
    conn = FakeConn()
    launcher = make_launcher(conn=conn)
    assert launcher._read_silent_hang_minutes() == DEFAULT_SILENT_HANG_MINUTES == 30


def test_read_silent_hang_minutes_invalid_falls_back_to_default() -> None:
    """Non-int / non-positive config values fall back to the default.

    A misconfigured admin (``"banana"``, ``"-5"``) must not break the
    detector or wedge the watch loop.
    """
    conn = FakeConn()
    _install_silent_hang_config(conn, minutes=0)  # zero is invalid
    launcher = make_launcher(conn=conn)
    assert launcher._read_silent_hang_minutes() == DEFAULT_SILENT_HANG_MINUTES


def test_silent_hang_log_stream_name_built_from_task_arn() -> None:
    """The log stream name is ``<prefix>/task-runner/<task-id>``.

    Task ARN format is
    ``arn:aws:ecs:<region>:<acct>:task/<cluster>/<task-id>``; the
    last URI segment is the task-id.
    """
    launcher = make_launcher(task_runner_log_stream_prefix="task-runner")
    name = launcher._build_log_stream_name(
        "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abcd1234beef5678"
    )
    assert name == "task-runner/task-runner/abcd1234beef5678"


def test_silent_hang_skips_stream_with_mismatched_name() -> None:
    """A prefix collision returning a different stream returns None.

    If two streams happen to share a common prefix, ``DescribeLogStreams``
    might return a different stream than we asked for. The detector
    must not interpret a *different* stream's stale timestamp as our
    task's silence.
    """
    conn = FakeConn()
    _install_running_agent_row(conn)
    _install_silent_hang_config(conn, minutes=None)

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }
    cw = MagicMock()
    cw.describe_log_streams.return_value = {
        "logStreams": [
            {
                "logStreamName": "task-runner/task-runner/different-task-id",
                "lastEventTimestamp": 0,  # ancient — would reap if matched
            }
        ]
    }

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        cloudwatch_logs_client=cw,
        task_runner_log_group="/ecs/judgemind-task-runner-dev",
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()
    assert transitions == []
    ecs.stop_task.assert_not_called()


# ---------------------------------------------------------------------------
# Claim budget exhaustion
# ---------------------------------------------------------------------------


def test_budget_exhaustion_skips_and_marks_needs_human() -> None:
    """When prior attempts >= claim_attempts_max, skip and label needs-human.

    The acceptance criterion calls out that the budget query has no
    ``parent_run_id`` filter — restarting the launcher does not reset
    the budget. ``_count_prior_attempts`` is the implementation; this
    test pins the user-visible behavior.
    """
    conn = FakeConn()

    # cap=1, so we have a slot; budget=3 (default — the missing
    # claim_attempts_max key falls through to the default); prior
    # attempts=3 (budget exhausted).
    def config_handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        key = params[0] if params else ""
        if key == "concurrency_cap_v3":
            cur._next_fetchone = ("1",)
        elif key == "claim_attempts_max":
            # Leave unset so the launcher falls back to the default —
            # this is the realistic dev environment shape.
            cur._next_fetchone = None
        else:
            cur._next_fetchone = None

    conn.install_handler("FROM dispatcher.config WHERE key = %s", config_handler)

    counts = iter(
        [(0,), (3,)]
    )  # first call: running v3 = 0; second: prior attempts = 3
    conn.install_handler(
        "COUNT(*) FROM dispatcher.agents",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", next(counts)),
    )

    ecs = MagicMock()
    invocations: list[list[str]] = []

    def fake_subprocess(cmd: list[str], **kwargs: Any) -> Any:
        invocations.append(cmd)
        if cmd[1:3] == ["issue", "list"]:
            return MagicMock(
                returncode=0,
                stdout=(
                    '[{"number": 9001, "title": "Stuck issue", '
                    '"labels": [{"name": "agent/ready"}], '
                    '"createdAt": "2026-05-01T00:00:00Z"}]'
                ),
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        subprocess_runner=fake_subprocess,
        trust_checker=lambda n: True,
    )
    claims, skipped = launcher._claim_if_cap_allows()

    assert claims == []
    assert len(skipped) == 1
    assert skipped[0]["number"] == 9001
    assert skipped[0]["reason"] == "budget_exhausted"
    assert skipped[0]["attempts"] == 3
    assert skipped[0]["limit"] == DEFAULT_CLAIM_ATTEMPTS_MAX
    # ``status/needs-human`` was added; ``ecs:RunTask`` not called; no
    # INSERT against dispatcher.agents fired.
    assert any(
        cmd[1:4] == ["issue", "edit", "9001"]
        and "--add-label" in cmd
        and LABEL_STATUS_NEEDS_HUMAN in cmd[-1]
        for cmd in invocations
    )
    ecs.run_task.assert_not_called()
    assert not any(
        sql.startswith("INSERT INTO dispatcher.agents") for sql, _ in conn.executed
    )


def test_budget_query_omits_parent_run_id_filter() -> None:
    """The per-issue claim budget intentionally has no parent_run_id filter.

    Restarting the launcher mints a new ``run_id``; if the budget were
    scoped to ``parent_run_id`` it would silently reset and a
    persistently broken issue could chew through unbounded attempts
    across deploys. The v3 spec calls this out and #3880's third AC
    pins it. The ``_count_prior_attempts`` SQL is the source of truth.
    """
    conn = FakeConn()
    conn.install_handler(
        "COUNT(*) FROM dispatcher.agents",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (2,)),
    )
    launcher = make_launcher(conn=conn)
    launcher._count_prior_attempts(9001)
    # Find the SELECT that ran for this call.
    matching = [
        (sql, params)
        for sql, params in conn.executed
        if "COUNT(*) FROM dispatcher.agents" in sql and "issue_number" in sql
    ]
    assert matching, "_count_prior_attempts must execute a COUNT against agents"
    sql, params = matching[-1]
    # The query must filter by issue_number AND by v3 dispatcher_version
    # (so v2 attempts don't bleed in) but must NOT carry an equality on
    # parent_run_id = self._run_id.
    assert "issue_number" in sql
    assert "dispatcher_version = 'v3'" in sql
    assert "parent_run_id = %s" not in sql
    # Params: the only bound value is the issue number.
    assert params == (9001,)


# ---------------------------------------------------------------------------
# Partial claim recovery
# ---------------------------------------------------------------------------


def test_recover_partial_claims_marks_old_rows_failed() -> None:
    conn = FakeConn()

    def update_rowcount(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        cur.rowcount = 2

    conn.install_handler(
        "UPDATE dispatcher.agents SET status = 'failed'",
        update_rowcount,
    )

    launcher = make_launcher(conn=conn)
    recovered = launcher._recover_partial_claims()
    assert recovered == 2
    # The UPDATE used the configured age threshold.
    matching = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("UPDATE dispatcher.agents SET status = 'failed'")
    ]
    assert matching, "recover_partial_claims must run an UPDATE"
    sql, params = matching[-1]
    assert "current_milestone = 'claiming'" in sql
    assert "task_arn IS NULL" in sql
    assert "exit_reason = 'claim_abandoned'" in sql
    assert params == (PARTIAL_CLAIM_RECOVERY_AGE_SECONDS,)


# ---------------------------------------------------------------------------
# v2/v3 cohabitation — skip filter
# ---------------------------------------------------------------------------


def test_dispatcher_v2_only_label_skips_claim() -> None:
    """Issues carrying ``dispatcher/v2-only`` are skipped before trust check."""
    conn = FakeConn()
    conn.install_handler(
        "FROM dispatcher.config WHERE key = %s",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", ("1",)),
    )
    conn.install_handler(
        "COUNT(*) FROM dispatcher.agents",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (0,)),
    )

    trust_called: list[int] = []
    ecs = MagicMock()
    invocations: list[list[str]] = []

    def fake_subprocess(cmd: list[str], **kwargs: Any) -> Any:
        invocations.append(cmd)
        if cmd[1:3] == ["issue", "list"]:
            return MagicMock(
                returncode=0,
                stdout=(
                    '[{"number": 7000, "title": "v2-only issue", '
                    '"labels": [{"name": "agent/ready"}, '
                    f'{{"name": "{LABEL_DISPATCHER_V2_ONLY}"}}], '
                    '"createdAt": "2026-05-01T00:00:00Z"}]'
                ),
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    def trust_checker(n: int) -> bool:
        trust_called.append(n)
        return True

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        subprocess_runner=fake_subprocess,
        trust_checker=trust_checker,
    )
    claims, skipped = launcher._claim_if_cap_allows()

    assert claims == []
    assert len(skipped) == 1
    assert skipped[0] == {"number": 7000, "reason": "v2_only"}
    # Trust check NOT consulted — the v2-only filter runs first so we
    # don't burn an API call on issues we won't claim.
    assert trust_called == []
    ecs.run_task.assert_not_called()


# ---------------------------------------------------------------------------
# Cap = 0 short-circuit
# ---------------------------------------------------------------------------


def test_cap_zero_skips_queue_scan_entirely() -> None:
    """When cap=0 the launcher does not even scan the queue.

    Default during cohabitation ramp (#3880 spec §9 step 2) — the v3
    daemon deploys at cap=0 so it is "present but not claiming".
    """
    conn = FakeConn()
    conn.install_handler(
        "FROM dispatcher.config WHERE key = %s",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", ("0",)),
    )
    invocations: list[list[str]] = []

    def fake_subprocess(cmd: list[str], **kwargs: Any) -> Any:
        invocations.append(cmd)
        return MagicMock(returncode=0, stdout="[]", stderr="")

    launcher = make_launcher(conn=conn, subprocess_runner=fake_subprocess)
    claims, skipped = launcher._claim_if_cap_allows()
    assert claims == []
    assert skipped == []
    # No gh issue list call was issued.
    assert all(cmd[1:3] != ["issue", "list"] for cmd in invocations)


def test_default_cap_is_zero_when_config_missing() -> None:
    """``concurrency_cap_v3`` row missing → DEFAULT_CONCURRENCY_CAP_V3 (0)."""
    conn = FakeConn()
    # No handler installed → fetchone returns None.
    launcher = make_launcher(conn=conn)
    cap = launcher._read_concurrency_cap_v3()
    assert cap == DEFAULT_CONCURRENCY_CAP_V3 == 0


def test_default_claim_attempts_max_when_config_missing() -> None:
    conn = FakeConn()
    launcher = make_launcher(conn=conn)
    assert launcher._read_claim_attempts_max() == DEFAULT_CLAIM_ATTEMPTS_MAX == 3


# ---------------------------------------------------------------------------
# Tick smoke — full loop with no work to do
# ---------------------------------------------------------------------------


def test_tick_no_work_runs_clean() -> None:
    """A tick with no commands, no in-flight tasks, cap=0 returns empty summary."""
    conn = FakeConn()
    # cap = 0
    conn.install_handler(
        "FROM dispatcher.config WHERE key = %s",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", ("0",)),
    )
    launcher = make_launcher(
        conn=conn,
        subprocess_runner=lambda *a, **k: MagicMock(
            returncode=0, stdout="[]", stderr=""
        ),
    )
    summary = launcher.tick()
    assert summary["commands_consumed"] == 0
    assert summary["heartbeat"] is True
    assert summary["watched"] == 0
    assert summary["transitions"] == []
    assert summary["claims"] == []


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_updates_run_row() -> None:
    conn = FakeConn()
    launcher = make_launcher(conn=conn)
    assert launcher._heartbeat() is True
    assert any(
        sql.startswith("UPDATE dispatcher.runs SET heartbeat_ts")
        and params == ("run-test-uuid",)
        for sql, params in conn.executed
    )


# ---------------------------------------------------------------------------
# Commands handler
# ---------------------------------------------------------------------------


def test_consume_commands_updates_concurrency_cap_v3() -> None:
    """The set_cap command writes ``concurrency_cap_v3``, NOT ``concurrency_cap``."""
    conn = FakeConn()
    conn.install_handler(
        "SELECT command_id, command, payload",
        lambda cur, sql, params: setattr(
            cur,
            "_next_fetchall",
            [(1, "set_cap", {"cap": 3})],
        ),
    )
    launcher = make_launcher(conn=conn)
    consumed = launcher._consume_commands()
    assert consumed == 1
    # The UPSERT writes the v3 key, not v2's.
    matching = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("INSERT INTO dispatcher.config") and params
    ]
    assert matching, "set_cap must write to dispatcher.config"
    _, params = matching[-1]
    assert params == ("concurrency_cap_v3", "3"), (
        "v3 must write its own cap key so v2 and v3 caps stay independent "
        "during cohabitation"
    )


def test_consume_commands_unknown_is_logged_and_consumed() -> None:
    conn = FakeConn()
    conn.install_handler(
        "SELECT command_id, command, payload",
        lambda cur, sql, params: setattr(
            cur,
            "_next_fetchall",
            [(2, "garbage", {})],
        ),
    )
    launcher = make_launcher(conn=conn)
    consumed = launcher._consume_commands()
    assert consumed == 1  # consumed (so it doesn't block the queue)
    # No config write for unknown commands.
    assert not any(
        sql.startswith("INSERT INTO dispatcher.config") for sql, _ in conn.executed
    )


# ---------------------------------------------------------------------------
# Wall-clock-cap detector (issue #3940)
# ---------------------------------------------------------------------------
#
# Pre-#3940 the wall-clock cap (default 6h) was rendered into the
# task-def's ``stopTimeout`` field. Fargate rejected those task-defs
# (it caps stopTimeout at 120s), so every dev-apply since F2 landed
# failed. The fix moves enforcement to the launcher: ``_watch_in_flight``
# now StopTask's any RUNNING task whose ``now - started_at`` exceeds
# the per-task-def cap, marking the agent failed with
# ``exit_reason='wall_clock_exceeded'``.
#
# These tests pin the contract:
#   * AC#2: "Launcher silent-hang detector reads the per-task-def cap
#     from the chosen source (tags / config / constants)." Source is
#     env-var → constructor (env-var driven, see _build_launcher_from_env).
#     test_wall_clock_constructor_arg_is_threaded confirms the value
#     reaches the watch loop and gates reap behavior.


def test_wall_clock_reaps_task_older_than_cap() -> None:
    """RUNNING task whose started_at is older than the cap gets reaped.

    The wall-clock cap is the structural replacement for the Fargate
    stopTimeout-driven kill (which Fargate rejects at >120s). Pins:
    (1) ecs:StopTask called with the right ARN; (2) the agent row's
    UPDATE used ``status='failed'`` and
    ``exit_reason='wall_clock_exceeded'``; (3) the transition surfaced
    by the watch loop carries the new exit_reason; (4) the diagnoser
    is launched (non-success terminal).
    """
    from datetime import datetime, timedelta, timezone

    conn = FakeConn()
    # 2h ago with a 1h cap → should reap.
    started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    _install_running_agent_row(conn, started_at=started_at)

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }
    # RunTask shape for the diagnoser launch path.
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:0:task/jm/diag-1"}],
        "failures": [],
    }

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        # 1h cap so the 2h started_at trips it.
        task_runner_wall_clock_seconds=3600,
        # Silent-hang detector OFF (default) so the wall-clock path is
        # the only reap mechanism this test exercises.
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    watched, transitions = launcher._watch_in_flight()

    assert watched == 1
    # ecs:StopTask called with the wall_clock_exceeded reason prefix.
    assert ecs.stop_task.call_count == 1
    stop_kwargs = ecs.stop_task.call_args.kwargs
    assert stop_kwargs["task"] == "arn:aws:ecs:us-west-2:0:task/jm/abc123def456"
    assert stop_kwargs["reason"].startswith("wall_clock_exceeded:")
    # Agent row UPDATE marks failed with the new exit_reason.
    matching = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("UPDATE dispatcher.agents") and "SET status = %s" in sql
    ]
    assert matching, "Expected an UPDATE to dispatcher.agents on wall-clock reap"
    _, params = matching[-1]
    assert params[0] == "failed"
    assert params[2] == EXIT_REASON_WALL_CLOCK_EXCEEDED
    # Transition surfaced to the caller.
    assert len(transitions) == 1
    assert transitions[0]["status"] == "failed"
    assert transitions[0]["exit_reason"] == EXIT_REASON_WALL_CLOCK_EXCEEDED
    # Diagnoser launched (non-success terminal triggers diagnosis).
    assert ecs.run_task.call_count == 1
    assert "diagnoser_arn" in transitions[0]


def test_wall_clock_skips_task_younger_than_cap() -> None:
    """RUNNING task within the wall-clock budget is left alone."""
    from datetime import datetime, timedelta, timezone

    conn = FakeConn()
    # 30min ago with a 6h cap → should NOT reap.
    started_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    _install_running_agent_row(conn, started_at=started_at)

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        # Default 6h cap; 30min started_at is well under.
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()

    assert transitions == []
    ecs.stop_task.assert_not_called()
    assert not any(
        sql.startswith("UPDATE dispatcher.agents") and "SET status = %s" in sql
        for sql, _ in conn.executed
    )


def test_wall_clock_skipped_when_cap_non_positive() -> None:
    """Cap <= 0 disables the wall-clock check entirely.

    Graceful degradation for tests / local CLI runs / environments
    before F2 wired the env var. Pins the AC: a bad env-var override
    (``"0"``) does not flip a healthy agent to failed.
    """
    from datetime import datetime, timedelta, timezone

    conn = FakeConn()
    # 100h ago — would reap under any positive cap.
    started_at = datetime.now(timezone.utc) - timedelta(hours=100)
    _install_running_agent_row(conn, started_at=started_at)

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }

    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        task_runner_wall_clock_seconds=0,  # disabled
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()

    assert transitions == []
    ecs.stop_task.assert_not_called()


def test_wall_clock_skipped_when_started_at_missing() -> None:
    """A row with NULL started_at must not be reaped.

    Defensive guard: a misshapen row (concurrent migration, replication
    lag) must not flip a healthy agent to failed.
    """
    conn = FakeConn()
    _install_running_agent_row(conn, started_at=None)

    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:aws:ecs:us-west-2:0:task/jm/abc123def456",
                "lastStatus": "RUNNING",
                "containers": [{}],
            }
        ]
    }
    launcher = make_launcher(
        conn=conn,
        ecs_client=ecs,
        task_runner_wall_clock_seconds=1,  # tiny cap
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )
    _, transitions = launcher._watch_in_flight()

    assert transitions == []
    ecs.stop_task.assert_not_called()


def test_wall_clock_naive_datetime_treated_as_utc() -> None:
    """A naive ``started_at`` (no tzinfo) is interpreted as UTC.

    psycopg returns timezone-aware datetimes by default for ``timestamptz``
    columns, but tests / older drivers may pass naive datetimes. The
    helper must treat them as UTC (matching the v2 daemon's convention)
    rather than interpreting in the local zone.
    """
    from datetime import datetime, timedelta, timezone

    # Build a naive datetime by stripping tzinfo from a UTC value (the
    # spelling that survives Python 3.13+'s deprecation of utcnow()).
    naive_old = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None)
    # Confirm the helper sees this as exceeding a 1h cap.
    launcher = make_launcher()
    assert launcher._task_age_exceeds_cap(started_at=naive_old, cap_seconds=3600)
    # And that a recent naive datetime does NOT exceed.
    naive_recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(
        tzinfo=None
    )
    assert not launcher._task_age_exceeds_cap(started_at=naive_recent, cap_seconds=3600)


def test_wall_clock_exit_reason_is_distinct_from_silent_hang() -> None:
    """The two reap classes write different exit_reason strings.

    Pin the literal so the diagnoser SKILL (which branches on
    ``exit_reason`` per spec §4.2) sees them as distinct kill classes.
    """
    assert EXIT_REASON_WALL_CLOCK_EXCEEDED == "wall_clock_exceeded"
    assert EXIT_REASON_SILENT_HANG == "silent_hang"
    assert EXIT_REASON_WALL_CLOCK_EXCEEDED != EXIT_REASON_SILENT_HANG


def test_wall_clock_default_constants_match_spec() -> None:
    """Pin the default cap values per spec §11 OQ#4 + issue body.

    A future change that bumps these defaults must do so deliberately
    (the test fails loudly) -- the spec values are referenced by
    F2's variables.tf docstrings and the dev-apply error messages.
    """
    assert DEFAULT_TASK_RUNNER_WALL_CLOCK_SECONDS == 21600  # 6h
    assert DEFAULT_DIAGNOSER_WALL_CLOCK_SECONDS == 3600  # 1h
    assert DEFAULT_SCHEDULED_SKILL_WALL_CLOCK_SECONDS == 7200  # 2h


def test_wall_clock_check_handles_non_datetime_started_at() -> None:
    """A non-datetime ``started_at`` (string, int) returns False.

    Defensive guard against a future migration that changes the column
    type or a buggy driver returning unexpected types. Must never reap
    on garbage input.
    """
    launcher = make_launcher()
    # Strings, ints, None, lists — all must return False.
    assert not launcher._task_age_exceeds_cap(
        started_at="2026-04-29T12:00:00Z", cap_seconds=1
    )
    assert not launcher._task_age_exceeds_cap(started_at=12345, cap_seconds=1)
    assert not launcher._task_age_exceeds_cap(started_at=None, cap_seconds=1)
    assert not launcher._task_age_exceeds_cap(started_at=[], cap_seconds=1)


# ---------------------------------------------------------------------------
# _parse_int_env helper (#3940 env-var parsing)
# ---------------------------------------------------------------------------


def test_parse_int_env_returns_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FAKE_WALL_CLOCK_VAR", raising=False)
    assert _parse_int_env("FAKE_WALL_CLOCK_VAR", 999) == 999


def test_parse_int_env_returns_default_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_WALL_CLOCK_VAR", "not-a-number")
    assert _parse_int_env("FAKE_WALL_CLOCK_VAR", 999) == 999


def test_parse_int_env_returns_default_on_zero_when_fallback_on_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fallback_on_zero=True`` rejects ``"0"`` and ``"-5"``.

    Pins the AC: a typo of ``"0"`` for a wall-clock cap must not
    silently disable the cap -- it falls back to the documented default.
    """
    monkeypatch.setenv("FAKE_WALL_CLOCK_VAR", "0")
    assert _parse_int_env("FAKE_WALL_CLOCK_VAR", 999, fallback_on_zero=True) == 999
    monkeypatch.setenv("FAKE_WALL_CLOCK_VAR", "-100")
    assert _parse_int_env("FAKE_WALL_CLOCK_VAR", 999, fallback_on_zero=True) == 999


def test_parse_int_env_accepts_valid_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_WALL_CLOCK_VAR", "120")
    assert _parse_int_env("FAKE_WALL_CLOCK_VAR", 999) == 120


def test_build_launcher_from_env_threads_wall_clock_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: env vars → constructor args → instance attributes.

    Pins the AC: F2's task-def renders ``TASK_RUNNER_WALL_CLOCK_SECONDS``
    etc. into the launcher container's environment; this method must
    parse them back into the launcher's wall-clock-cap fields.
    """
    from dispatcher_v3.launcher import _build_launcher_from_env

    monkeypatch.setenv("ECS_CLUSTER_ARN", "arn:aws:ecs:us-west-2:0:cluster/jm")
    monkeypatch.setenv("TASK_RUNNER_TASK_DEFINITION", "judgemind-task-runner:7")
    monkeypatch.setenv("AGENT_RUNNER_SECURITY_GROUP_ID", "sg-aaa")
    monkeypatch.setenv("SESSIONS_BUCKET", "judgemind-sessions-dev")
    monkeypatch.setenv("AGENT_RUNNER_SUBNET_IDS", "subnet-a,subnet-b")
    monkeypatch.setenv("TASK_RUNNER_WALL_CLOCK_SECONDS", "1234")
    monkeypatch.setenv("DIAGNOSER_WALL_CLOCK_SECONDS", "567")
    monkeypatch.setenv("SCHEDULED_SKILL_WALL_CLOCK_SECONDS", "89")

    launcher = _build_launcher_from_env()
    assert launcher._task_runner_wall_clock_seconds == 1234
    assert launcher._diagnoser_wall_clock_seconds == 567
    assert launcher._scheduled_skill_wall_clock_seconds == 89


def test_build_launcher_from_env_falls_back_when_wall_clock_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset env vars → defaults (graceful boot before F2 re-applies)."""
    from dispatcher_v3.launcher import _build_launcher_from_env

    monkeypatch.setenv("ECS_CLUSTER_ARN", "arn:aws:ecs:us-west-2:0:cluster/jm")
    monkeypatch.setenv("TASK_RUNNER_TASK_DEFINITION", "judgemind-task-runner:7")
    monkeypatch.setenv("AGENT_RUNNER_SECURITY_GROUP_ID", "sg-aaa")
    monkeypatch.setenv("SESSIONS_BUCKET", "judgemind-sessions-dev")
    monkeypatch.setenv("AGENT_RUNNER_SUBNET_IDS", "subnet-a")
    monkeypatch.delenv("TASK_RUNNER_WALL_CLOCK_SECONDS", raising=False)
    monkeypatch.delenv("DIAGNOSER_WALL_CLOCK_SECONDS", raising=False)
    monkeypatch.delenv("SCHEDULED_SKILL_WALL_CLOCK_SECONDS", raising=False)

    launcher = _build_launcher_from_env()
    assert (
        launcher._task_runner_wall_clock_seconds
        == DEFAULT_TASK_RUNNER_WALL_CLOCK_SECONDS
    )
    assert (
        launcher._diagnoser_wall_clock_seconds == DEFAULT_DIAGNOSER_WALL_CLOCK_SECONDS
    )
    assert (
        launcher._scheduled_skill_wall_clock_seconds
        == DEFAULT_SCHEDULED_SKILL_WALL_CLOCK_SECONDS
    )
