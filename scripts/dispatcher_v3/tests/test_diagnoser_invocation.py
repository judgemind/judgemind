"""Unit tests for the launcher's diagnoser-invocation path (issue #3882).

The launcher spawns a one-shot ``diagnoser`` ECS task whenever a
task-runner ECS task exits non-zero (or is killed by silent-hang
detection — that path lands in C4 / #3881). The diagnoser SKILL reads
the agent's session log + the issue/PR state, performs side-effect
decisions directly (close / re-add ``agent/ready`` / file follow-up /
mark ``status/needs-human``), writes ``agents.outcome_summary``, and
exits.

This module pins the launcher-side invariants (spec §4.2):

- **Failure → diagnoser task launched** with ``AGENT_ID`` env override on
  the diagnoser container, ``diagnoser_arn`` persisted to the agent row.
- **Cap of 1 per agent** — once ``diagnoser_arn IS NOT NULL`` the
  launcher does not launch another diagnoser, even on subsequent ticks
  while the first is still RUNNING. Spec language: "no recursion of
  'diagnose the diagnoser.'"
- **STOPPED-0 is a no-op.** The diagnoser SKILL has already written
  ``outcome_summary`` and (optionally) re-added ``agent/ready``. The
  launcher only writes a defensive fallback sentinel if the column is
  still null, so the watch query stops re-matching the row.
- **STOPPED-non-zero → status='needs_review'.** The diagnoser failed
  itself (OOM / spot-reclaim / crash). The agent is bumped to
  ``needs_review`` and a TODO marker for the C6 Telegram alert is in
  place.
- **Force-kill cascades.** ``force_kill <agent_id>`` calls
  ``ecs:StopTask`` against both ``task_arn`` and ``diagnoser_arn`` if
  non-null — prevents an orphan diagnoser running after the operator
  has decided.

Each test uses the same FakeConn + MagicMock-ECS plumbing as
``test_launcher.py``; the shared helpers are duplicated here for
locality (the v3 tests are < 200 LOC each, so the cost is small and
the boundary is cleaner than re-exporting from another test module).
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

# Inject a fake ``psycopg.errors`` module up front so the launcher's
# lazy ``import psycopg`` finds something callable in environments
# where the real wheel is not installed (CI test runner per
# ``.github/workflows/ci.yml``). Same shim as test_launcher.py.
if "psycopg" not in sys.modules:
    fake_psycopg = types.ModuleType("psycopg")
    fake_errors = types.ModuleType("psycopg.errors")

    class _FakeUniqueViolation(Exception):
        pass

    fake_errors.UniqueViolation = _FakeUniqueViolation
    fake_psycopg.errors = fake_errors
    sys.modules["psycopg"] = fake_psycopg
    sys.modules["psycopg.errors"] = fake_errors


from dispatcher_v3.launcher import Launcher  # noqa: E402 — must follow sys.modules patch


# ---------------------------------------------------------------------------
# Fakes — duplicated from test_launcher.py for locality. See module docstring.
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
        for predicate, handler in self.conn.handlers:
            if predicate in sql:
                handler(self, sql, params or ())
                return
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
        self.handlers.append((predicate, handler))


def make_launcher(
    conn: FakeConn | None = None,
    ecs_client: MagicMock | None = None,
    diagnoser_task_definition: str = "judgemind-dispatcher-v3-diagnoser",
) -> Launcher:
    """Construct a :class:`Launcher` with diagnoser-test defaults."""
    return Launcher(
        run_id="run-test-uuid",
        github_repo="judgemind/judgemind",
        ecs_cluster_arn="arn:aws:ecs:us-west-2:0:cluster/jm",
        task_runner_task_definition="judgemind-task-runner:7",
        diagnoser_task_definition=diagnoser_task_definition,
        agent_runner_subnet_ids=["subnet-a", "subnet-b"],
        agent_runner_security_group_id="sg-aaa",
        sessions_bucket="judgemind-sessions-dev",
        conn=conn,
        ecs_client=ecs_client,
        subprocess_runner=lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
    )


# ---------------------------------------------------------------------------
# Failure → diagnoser launch
# ---------------------------------------------------------------------------


def test_failure_launches_diagnoser_and_persists_arn() -> None:
    """STOPPED-non-zero in _watch_in_flight launches the diagnoser inline.

    The launcher reads the agent row, observes STOPPED with non-zero
    exit, marks the agent ``failed``, and immediately calls
    ``ecs:RunTask`` against the diagnoser task-def with ``AGENT_ID``
    set. The returned task ARN is persisted to ``diagnoser_arn``.
    """
    conn = FakeConn()
    # _watch_in_flight's SELECT returns one in-flight task.
    conn.install_handler(
        "SELECT agent_id, task_arn, issue_number FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur, "_next_fetchall", [("ag-1", "arn:task/run-1", 100)],
        ),
    )
    # _launch_diagnoser's cap-check SELECT returns no prior diagnoser.
    conn.install_handler(
        "SELECT diagnoser_arn FROM dispatcher.agents",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", (None,)),
    )

    ecs = MagicMock()
    # describe_tasks returns the failed task-runner.
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:task/run-1",
                "lastStatus": "STOPPED",
                "containers": [{"exitCode": 7}],
                "stoppedReason": "Container exited",
            },
        ],
    }
    # run_task returns the diagnoser task.
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:task/diag-1"}],
        "failures": [],
    }

    launcher = make_launcher(conn=conn, ecs_client=ecs)
    watched, transitions = launcher._watch_in_flight()

    assert watched == 1
    assert len(transitions) == 1
    assert transitions[0]["agent_id"] == "ag-1"
    assert transitions[0]["status"] == "failed"
    assert transitions[0]["exit_code"] == 7
    assert transitions[0]["diagnoser_arn"] == "arn:task/diag-1"

    # ecs:RunTask was called once for the diagnoser (separate from the
    # describe_tasks call). Inspect the call kwargs.
    assert ecs.run_task.call_count == 1
    kwargs = ecs.run_task.call_args.kwargs
    assert kwargs["taskDefinition"] == "judgemind-dispatcher-v3-diagnoser"
    assert kwargs["launchType"] == "FARGATE"
    container_overrides = kwargs["overrides"]["containerOverrides"]
    assert len(container_overrides) == 1
    # The container name in the diagnoser task-def is "diagnoser" — the
    # override must target it specifically.
    assert container_overrides[0]["name"] == "diagnoser"
    env_pairs = container_overrides[0]["environment"]
    env_by_name = {p["name"]: p["value"] for p in env_pairs}
    assert env_by_name == {"AGENT_ID": "ag-1"}, (
        "AGENT_ID is the only env override the launcher must pass — the "
        "diagnoser SKILL reads everything else from the DB row + S3 "
        "transcript per spec §4.2."
    )

    # The DB UPDATE for diagnoser_arn fired with the returned ARN.
    assert any(
        sql.startswith("UPDATE dispatcher.agents")
        and "SET diagnoser_arn = %s" in sql
        and params[0] == "arn:task/diag-1"
        and params[1] == "ag-1"
        for sql, params in conn.executed
    )


def test_failure_with_no_diagnoser_taskdef_does_not_launch() -> None:
    """Empty diagnoser_task_definition skips the launch (F2 not yet landed).

    During the dispatcher-v3 buildout F2 (#3887) ships the diagnoser
    task-def. Before it lands, the launcher boots with an empty env
    var; failures still mark the agent ``failed`` correctly but no
    diagnoser is spawned. The cohabitation cap=0 means failures are
    rare in this window anyway.
    """
    conn = FakeConn()
    conn.install_handler(
        "SELECT agent_id, task_arn, issue_number FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur, "_next_fetchall", [("ag-1", "arn:task/run-1", 100)],
        ),
    )
    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:task/run-1",
                "lastStatus": "STOPPED",
                "containers": [{"exitCode": 7}],
                "stoppedReason": "Container exited",
            },
        ],
    }
    launcher = make_launcher(
        conn=conn, ecs_client=ecs, diagnoser_task_definition=""
    )
    _, transitions = launcher._watch_in_flight()
    # Status transition still fired — the agent is correctly marked failed.
    assert len(transitions) == 1
    assert transitions[0]["status"] == "failed"
    # No diagnoser launched; no diagnoser_arn key on the transition.
    assert "diagnoser_arn" not in transitions[0]
    ecs.run_task.assert_not_called()


def test_success_does_not_launch_diagnoser() -> None:
    """STOPPED-exit-0 transitions to ``succeeded`` with no diagnoser launch."""
    conn = FakeConn()
    conn.install_handler(
        "SELECT agent_id, task_arn, issue_number FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur, "_next_fetchall", [("ag-1", "arn:task/run-1", 100)],
        ),
    )
    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:task/run-1",
                "lastStatus": "STOPPED",
                "containers": [{"exitCode": 0}],
                "stoppedReason": "Essential container exited",
            },
        ],
    }
    launcher = make_launcher(conn=conn, ecs_client=ecs)
    _, transitions = launcher._watch_in_flight()
    assert len(transitions) == 1
    assert transitions[0]["status"] == "succeeded"
    assert "diagnoser_arn" not in transitions[0]
    ecs.run_task.assert_not_called()


# ---------------------------------------------------------------------------
# Cap of 1 per agent
# ---------------------------------------------------------------------------


def test_cap_existing_diagnoser_arn_skips_relaunch() -> None:
    """Calling _launch_diagnoser when diagnoser_arn is already set is a no-op.

    Cap of 1 per agent (spec §4.2: "no recursion of 'diagnose the
    diagnoser.'"). The cap-check SELECT inside _launch_diagnoser sees
    a non-null diagnoser_arn and returns None without calling
    ecs:RunTask.
    """
    conn = FakeConn()
    conn.install_handler(
        "SELECT diagnoser_arn FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur, "_next_fetchone", ("arn:task/diag-existing",),
        ),
    )
    ecs = MagicMock()
    launcher = make_launcher(conn=conn, ecs_client=ecs)
    result = launcher._launch_diagnoser(agent_id="ag-1")
    assert result is None
    ecs.run_task.assert_not_called()


def test_cap_holds_across_repeated_failure_observations() -> None:
    """Two failure observations for the same agent only launch one diagnoser.

    Simulates the launcher observing the same STOPPED-non-zero task on
    two consecutive ticks (e.g. a clock-skew hiccup or a race in the
    watch loop). The second observation's _launch_diagnoser sees the
    diagnoser_arn that was persisted by the first call and short-circuits.
    """
    conn = FakeConn()

    # The cap-check SELECT toggles between "no diagnoser" (first call) and
    # "diagnoser exists" (subsequent calls) so we can simulate the second
    # tick observing the same row after the first tick wrote diagnoser_arn.
    cap_check_states = iter([(None,), ("arn:task/diag-1",)])

    def cap_check_handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        try:
            cur._next_fetchone = next(cap_check_states)
        except StopIteration:
            cur._next_fetchone = ("arn:task/diag-1",)

    conn.install_handler(
        "SELECT diagnoser_arn FROM dispatcher.agents",
        cap_check_handler,
    )
    ecs = MagicMock()
    ecs.run_task.return_value = {
        "tasks": [{"taskArn": "arn:task/diag-1"}],
        "failures": [],
    }
    launcher = make_launcher(conn=conn, ecs_client=ecs)

    first = launcher._launch_diagnoser(agent_id="ag-1")
    second = launcher._launch_diagnoser(agent_id="ag-1")

    assert first == "arn:task/diag-1"
    assert second is None
    # Only one ecs:RunTask call total.
    assert ecs.run_task.call_count == 1


# ---------------------------------------------------------------------------
# Diagnoser watch — STOPPED-0 path
# ---------------------------------------------------------------------------


def test_watch_diagnoser_stopped_zero_is_noop_for_status() -> None:
    """STOPPED-0 leaves the agent's status='failed' alone.

    Spec §4.2: "no further action — the diagnoser already wrote
    outcome_summary." The launcher writes a defensive fallback sentinel
    via COALESCE so that a future diagnoser write is never overwritten,
    and the watch predicate (outcome_summary IS NULL) stops re-matching
    the row.
    """
    conn = FakeConn()
    conn.install_handler(
        "SELECT agent_id, diagnoser_arn FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur, "_next_fetchall", [("ag-1", "arn:task/diag-1")],
        ),
    )
    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:task/diag-1",
                "lastStatus": "STOPPED",
                "containers": [{"exitCode": 0}],
                "stoppedReason": "Diagnoser ran cleanly",
            },
        ],
    }
    launcher = make_launcher(conn=conn, ecs_client=ecs)
    watched, transitions = launcher._watch_diagnosers()

    assert watched == 1
    assert len(transitions) == 1
    assert transitions[0]["agent_id"] == "ag-1"
    assert transitions[0]["diagnoser_status"] == "succeeded"
    # No ``status='needs_review'`` transition fired.
    assert not any(
        sql.startswith("UPDATE dispatcher.agents")
        and "status = 'needs_review'" in sql
        for sql, _ in conn.executed
    )
    # The fallback sentinel was written via COALESCE so a real diagnoser
    # outcome_summary is never overwritten.
    matching = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("UPDATE dispatcher.agents")
        and "outcome_summary = COALESCE(outcome_summary" in sql
    ]
    assert matching, (
        "STOPPED-0 must write a fallback outcome_summary sentinel via "
        "COALESCE so the watch query stops re-matching the row"
    )
    sql, params = matching[-1]
    assert params == ("diagnoser_completed", "ag-1")


def test_watch_diagnoser_running_is_noop() -> None:
    """A diagnoser still RUNNING produces no transition."""
    conn = FakeConn()
    conn.install_handler(
        "SELECT agent_id, diagnoser_arn FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur, "_next_fetchall", [("ag-1", "arn:task/diag-1")],
        ),
    )
    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:task/diag-1",
                "lastStatus": "RUNNING",
                "containers": [{}],
            },
        ],
    }
    launcher = make_launcher(conn=conn, ecs_client=ecs)
    watched, transitions = launcher._watch_diagnosers()
    assert watched == 1
    assert transitions == []
    # No status update fired.
    assert not any(
        sql.startswith("UPDATE dispatcher.agents") and "status =" in sql
        for sql, _ in conn.executed
    )


# ---------------------------------------------------------------------------
# Diagnoser watch — STOPPED-non-zero → needs_review
# ---------------------------------------------------------------------------


def test_watch_diagnoser_failure_marks_needs_review() -> None:
    """STOPPED-non-zero on the diagnoser flips agent to ``needs_review``.

    Spec §4.2: "if the diagnoser exits non-zero, OOMs, or is reclaimed
    → status='needs_review' and Telegram-alert." The launcher only
    handles the DB transition here; the C6 Telegram alert lives behind
    a TODO marker until #3883 lands.
    """
    conn = FakeConn()
    conn.install_handler(
        "SELECT agent_id, diagnoser_arn FROM dispatcher.agents",
        lambda cur, sql, params: setattr(
            cur, "_next_fetchall", [("ag-1", "arn:task/diag-1")],
        ),
    )
    ecs = MagicMock()
    ecs.describe_tasks.return_value = {
        "tasks": [
            {
                "taskArn": "arn:task/diag-1",
                "lastStatus": "STOPPED",
                "containers": [{"exitCode": 137}],  # SIGKILL / OOM
                "stoppedReason": "OutOfMemoryError: Container killed",
            },
        ],
    }
    launcher = make_launcher(conn=conn, ecs_client=ecs)
    _, transitions = launcher._watch_diagnosers()

    assert len(transitions) == 1
    assert transitions[0]["agent_id"] == "ag-1"
    assert transitions[0]["diagnoser_status"] == "failed"
    assert transitions[0]["diagnoser_exit_code"] == 137
    assert "OutOfMemoryError" in transitions[0]["diagnoser_exit_reason"]

    # The UPDATE flips status to needs_review.
    matching = [
        (sql, params)
        for sql, params in conn.executed
        if sql.startswith("UPDATE dispatcher.agents")
        and "status = 'needs_review'" in sql
    ]
    assert matching, "diagnoser failure must flip status to needs_review"
    sql, params = matching[-1]
    # outcome_summary captures the failure context for the cockpit.
    assert "outcome_summary = COALESCE(outcome_summary" in sql
    assert params[0].startswith("diagnoser_failed: exit=137")
    assert "OutOfMemoryError" in params[0]
    assert params[1] == "ag-1"


def test_diagnoser_watch_predicate_filters_completed_rows() -> None:
    """The watch SELECT excludes rows whose diagnoser already resolved.

    Predicate: ``diagnoser_arn IS NOT NULL AND status='failed' AND
    outcome_summary IS NULL``. Once outcome_summary is non-null (the
    diagnoser SKILL wrote it on success, or our fallback wrote
    ``diagnoser_completed`` on STOPPED-0, or _mark_agent_needs_review
    wrote a failure summary on STOPPED-non-zero), the row drops out of
    the scan. Pinned because the predicate is the gate that prevents
    forever-rescanning a STOPPED-0 diagnoser.
    """
    conn = FakeConn()
    captured_sql: list[str] = []

    def select_handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        captured_sql.append(sql)
        cur._next_fetchall = []

    conn.install_handler(
        "SELECT agent_id, diagnoser_arn FROM dispatcher.agents",
        select_handler,
    )
    launcher = make_launcher(conn=conn, ecs_client=MagicMock())
    launcher._watch_diagnosers()

    assert captured_sql, "watch must execute the SELECT"
    sql = captured_sql[-1]
    assert "diagnoser_arn IS NOT NULL" in sql
    assert "status = 'failed'" in sql
    assert "outcome_summary IS NULL" in sql
    # And it's scoped to v3 runs so a v2-owned row doesn't bleed in.
    assert "dispatcher_version = 'v3'" in sql


# ---------------------------------------------------------------------------
# Force-kill cascade
# ---------------------------------------------------------------------------


def test_force_kill_cascades_to_both_arns() -> None:
    """``force_kill`` calls ``ecs:StopTask`` against both task_arn AND diagnoser_arn.

    Spec §4.2: "force-kill cascades to the diagnoser if one is running
    (ecs:StopTask on both task_arn and diagnoser_arn). This prevents an
    orphan diagnoser running after the operator has decided."

    The agent row in this scenario carries ``ended_at IS NULL`` (the
    task is still in flight) AND ``diagnoser_arn`` set (a diagnoser was
    spawned, possibly from a hypothetical earlier transition the test
    isn't simulating in detail). Force-kill must hit both.
    """
    conn = FakeConn()

    def select_handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        # Row: (task_arn, diagnoser_arn, ended_at)
        cur._next_fetchone = ("arn:task/run-1", "arn:task/diag-1", None)

    conn.install_handler(
        "SELECT task_arn, diagnoser_arn, ended_at FROM dispatcher.agents",
        select_handler,
    )

    ecs = MagicMock()
    launcher = make_launcher(conn=conn, ecs_client=ecs)
    launcher._force_kill_agent("ag-1")

    # Two stop_task calls — one per ARN.
    assert ecs.stop_task.call_count == 2
    targets = [c.kwargs["task"] for c in ecs.stop_task.call_args_list]
    assert "arn:task/run-1" in targets
    assert "arn:task/diag-1" in targets
    # All calls share cluster + reason.
    for call in ecs.stop_task.call_args_list:
        assert call.kwargs["cluster"] == "arn:aws:ecs:us-west-2:0:cluster/jm"
        assert call.kwargs["reason"] == "force_kill"


def test_force_kill_only_diagnoser_when_task_already_ended() -> None:
    """Once the task has ended (ended_at set), force-kill targets only diagnoser.

    The agent's task-runner ECS task already STOPPED; firing StopTask
    against a stopped task is a no-op or error. The diagnoser is the
    only live target. This case fires when the operator force-kills an
    agent whose task crashed and a diagnoser is now in flight.
    """
    conn = FakeConn()

    def select_handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        # (task_arn, diagnoser_arn, ended_at)  — ended_at non-null.
        cur._next_fetchone = (
            "arn:task/run-1",
            "arn:task/diag-1",
            "2026-05-02T04:30:00Z",
        )

    conn.install_handler(
        "SELECT task_arn, diagnoser_arn, ended_at FROM dispatcher.agents",
        select_handler,
    )
    ecs = MagicMock()
    launcher = make_launcher(conn=conn, ecs_client=ecs)
    launcher._force_kill_agent("ag-1")
    # Only the diagnoser ARN was stopped.
    assert ecs.stop_task.call_count == 1
    assert ecs.stop_task.call_args.kwargs["task"] == "arn:task/diag-1"


def test_force_kill_no_arns_is_noop() -> None:
    """No task_arn and no diagnoser_arn → force_kill is a clean no-op."""
    conn = FakeConn()

    def select_handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        cur._next_fetchone = (None, None, None)

    conn.install_handler(
        "SELECT task_arn, diagnoser_arn, ended_at FROM dispatcher.agents",
        select_handler,
    )
    ecs = MagicMock()
    launcher = make_launcher(conn=conn, ecs_client=ecs)
    launcher._force_kill_agent("ag-1")
    ecs.stop_task.assert_not_called()


def test_force_kill_unknown_agent_is_noop() -> None:
    """Force-kill against a missing agent_id is a clean no-op."""
    conn = FakeConn()

    def select_handler(cur: FakeCursor, sql: str, params: tuple[Any, ...]) -> None:
        cur._next_fetchone = None  # no row

    conn.install_handler(
        "SELECT task_arn, diagnoser_arn, ended_at FROM dispatcher.agents",
        select_handler,
    )
    ecs = MagicMock()
    launcher = make_launcher(conn=conn, ecs_client=ecs)
    launcher._force_kill_agent("ag-missing")
    ecs.stop_task.assert_not_called()


# ---------------------------------------------------------------------------
# Tick-level smoke — diagnoser watch is part of the standard tick
# ---------------------------------------------------------------------------


def test_tick_summary_includes_diagnoser_keys() -> None:
    """``Launcher.tick`` populates the diagnoser-watch fields in its summary.

    Pinned so a future "we forgot to wire _watch_diagnosers into the
    tick" regression fails loudly. The summary is logged each tick
    via CloudWatch Logs Insights for cockpit observability.
    """
    conn = FakeConn()
    conn.install_handler(
        "FROM dispatcher.config WHERE key = %s",
        lambda cur, sql, params: setattr(cur, "_next_fetchone", ("0",)),
    )
    launcher = make_launcher(conn=conn, ecs_client=MagicMock())
    summary = launcher.tick()
    assert "diagnosers_watched" in summary
    assert "diagnoser_transitions" in summary
    assert summary["diagnosers_watched"] == 0
    assert summary["diagnoser_transitions"] == []
