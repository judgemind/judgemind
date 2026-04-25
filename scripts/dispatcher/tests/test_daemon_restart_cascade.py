"""Unit tests for the #2872 restart-cascade fix.

Issue #2872. The 2026-04-19 daemon restart mid-ralph produced a
six-sub-bug cascade (agent ``ee127c3c-4611-4ed2-8ffe-f1ad04064b45`` on
issue #2807). This module covers each sub-bug with a failing-before-fix
regression test so future regressions are caught:

* **Bug A — daemon restart re-spawns pre-existing in-flight phases.**
  Startup ``recover_abandoned_agents`` sweep reclaims ``status='running'``
  agents from prior runs, flips them to ``crashed``, and enqueues a
  restart-abandoned retry marker instead of depending on the 30m
  stuck_timeout.
* **Bug B — stuck_timeout threshold too aggressive.** The supervisor
  now reads a per-phase threshold (``STUCK_TIMEOUT_SECONDS_BY_PHASE``
  + ``dispatcher.config.stuck_timeout_s_by_phase`` override) so a
  2.5-minute ralph or 90-second plan never trips the timer.
* **Bug C — diagnoser JSON parser fallback.** Already covered by
  ``test_daemon_phase3d.py`` — extended here with a test that the
  mechanical escalation path still writes the DB terminal even when
  the ``status/needs-human`` label add fails.
* **Bug D — ``status/needs-human`` label creation.** New startup helper
  ``ensure_required_labels`` idempotently ``gh label create --force``s
  the label so the diagnoser's escalate path has an operator-visible
  signal.
* **Bug E — ``idx_dispatcher_phase_outputs_agent_phase`` unique
  collision.** ``_persist_phase_output`` now derives an ``attempt``
  column from ``dispatcher.agents.retries_used`` and writes it as part
  of the INSERT so second/third runs of the same phase don't silently
  lose output. Migration 30 widens the unique index to
  ``(agent_id, phase, attempt)``.
* **Bug F — worker thread doesn't respect DB-level terminal writes.**
  ``_check_killswitch_and_abort`` now also checks
  ``dispatcher.agents.status`` and aborts at the next phase boundary
  when it observes one of ``TERMINAL_AGENT_STATUSES`` (``failed``,
  ``crashed``, ``succeeded``, ``plan_blocked``, ``needs_review``).

All external calls are mocked. Tests share the same ``_FakeCursor`` +
``_FakeConnection`` helpers from ``test_daemon_phase3c`` — re-imported
here so the file is self-contained.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402 — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes
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


class _CapturingLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.restart_cascade.{id(tmp_path)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConnection()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake",
        version_sha="deadbee",
        host="test-host",
        pid=9999,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-test",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "new-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# Bug A — recover_abandoned_agents (daemon restart mid-phase)
# --------------------------------------------------------------------------


class TestRecoverAbandonedAgents:
    """Daemon boot reclaims ``status='running'`` agents from prior runs.

    Before #2872, these agents lingered as ``status='running'`` after
    their parent daemon died mid-phase. The supervisor's 30-minute
    stuck_timeout eventually caught them, but the stale
    ``phase_transitions`` entries produced the cascading retry loop.
    The restart-recovery sweep now reclaims them immediately at boot
    with a dedicated retry category.
    """

    def test_abandoned_agent_reclaimed_and_retry_marker_enqueued(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        # One abandoned agent from a prior run.
        conn.cursor_instance.fetchall_queue = [
            [("agent-abandoned", 2807, "ralph")],
        ]
        # _write_failure → _mark_agent_terminal → _create_retry_marker
        # reads nothing via fetchone in the write path except the
        # retry-marker COUNT query + backoff_seconds config. Post-#2927
        # the #2903 _lookup_agent_kind fetch is gone.
        conn.cursor_instance.fetch_queue = [
            (0,),  # prior retry marker count
            ("[60,300,900]",),  # backoff schedule
        ]

        reclaimed = d.recover_abandoned_agents()
        assert reclaimed == 1

        # Exactly one failure row written with the new category.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        assert (
            failure_inserts[0][1][1] == daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED
        )
        assert failure_inserts[0][1][2] == "boot_recovery"

        # Agent flipped to crashed with the restart-abandoned phase.
        agent_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and e[1] is not None
        ]
        assert agent_updates
        # One of the updates contains status='crashed' and
        # phase='daemon_restart_abandoned'.
        crashed_update = [
            e
            for e in agent_updates
            if "crashed" in e[1] and "daemon_restart_abandoned" in e[1]
        ]
        assert crashed_update, f"no crashed+restart update found in {agent_updates}"

        # Retry marker enqueued with the restart-abandoned reason.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        assert (
            marker_inserts[0][1][1] == daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED
        )

        # Structured log event emitted for CloudWatch.
        recovered_events = handler.events("agent_recovered_from_restart")
        assert len(recovered_events) == 1

    def test_no_abandoned_agents_returns_zero(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        assert d.recover_abandoned_agents() == 0
        assert handler.events("recover_no_abandoned")

    def test_same_run_agent_not_reclaimed(self, tmp_path: Path) -> None:
        """Agents with ``parent_run_id == current run_id`` are NOT reclaimed.

        The SELECT filters ``parent_run_id IS NULL OR parent_run_id <>
        current``. An agent owned by THIS daemon's run_id is in-flight
        under this daemon; recovery would steal it from its worker
        thread mid-phase.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        d.recover_abandoned_agents()
        # Confirm the SELECT query filter uses parent_run_id comparison.
        selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT" in e[0] and "dispatcher.agents" in e[0]
        ]
        assert selects
        sql, params = selects[0]
        assert "parent_run_id" in sql
        assert params == (d._run_id,)

    def test_db_error_returns_zero_without_raising(self, tmp_path: Path) -> None:
        """Startup must not block on a recovery sweep failure.

        The supervisor's stuck_timeout is the backstop — missing the
        immediate reclaim just means the agent waits for the 30m path
        (which, post-#2872, no longer cascades).
        """
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        assert d.recover_abandoned_agents() == 0
        assert conn.rollbacks >= 1

    def test_select_has_no_kind_filter(self, tmp_path: Path) -> None:
        """Post-#2927 regression: ``recover_abandoned_agents`` has no kind filter.

        The /task skill stopped writing to ``dispatcher.agents``
        (label-only coordination), so every ``status='running'`` row
        from a prior run is daemon-owned by construction. The #2908
        ``kind='task'`` carve-out was reverted — the SELECT is back
        to its pre-#2866 shape.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        d.recover_abandoned_agents()

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT agent_id, issue_number, phase" in e[0]
            and "dispatcher.agents" in e[0]
        ]
        assert selects, "expected recover_abandoned_agents to issue the SELECT"
        sql, _params = selects[0]
        assert "status = 'running'" in sql
        assert "kind = 'task'" not in sql, (
            "recover_abandoned_agents should not carry a kind filter "
            "post-#2927 — every row is daemon-owned. Actual SQL: " + sql
        )


# --------------------------------------------------------------------------
# Bug #3152 — ECS-mode agents must survive daemon restart
# --------------------------------------------------------------------------


class TestRecoverAbandonedAgentsEcsExecutionMode:
    """Daemon startup reconciliation branches on ``execution_mode`` (#3152).

    Before the fix, the startup sweep unconditionally marked every
    ``status='running'`` agent from a prior run as
    ``daemon_restart_abandoned``. For subprocess-mode agents this is
    correct — their worker subprocesses died with the old daemon. For
    ECS-mode agents (#3086/#3091) it is wrong: their agent-runner
    process runs in an independent Fargate task that outlives the
    daemon redeploy. The pre-fix behaviour defeated Option A's core
    promise — one daemon redeploy killed the first full-pipeline ECS
    agent (agent ``6e79fb52`` on #2671 at ~2h ralph) even though
    ``aws ecs describe-tasks`` confirmed the task was still RUNNING.

    The fix: branch on ``execution_mode``. Subprocess path is
    unchanged. ECS path emits an ``agent_ecs_survived_daemon_restart``
    INFO event, takes no DB action, and defers liveness observation
    to :meth:`_reap_completed_agent_tasks` on the next scheduler tick.
    """

    def test_ecs_agent_survives_daemon_restart(self, tmp_path: Path) -> None:
        """ECS-mode row emits survival event, no retry marker, no terminal."""
        d, conn, handler = _make_daemon(tmp_path)

        # One ECS-mode agent in-flight at daemon startup.
        task_arn = (
            "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/"
            "eb57861c531e41359d99e18c95d86e0e"
        )
        conn.cursor_instance.fetchall_queue = [
            [("agent-ecs", 2671, "ralph", "ecs", task_arn)],
        ]
        # The ECS branch issues no fetchone calls — no retry-marker
        # lookup, no backoff config read, no agent-kind lookup. Queue
        # empty to prove it.
        conn.cursor_instance.fetch_queue = []

        reclaimed = d.recover_abandoned_agents()
        # ECS agents are NOT counted in the "reclaimed" return — the
        # return value reports subprocess-mode reclaims only (agents
        # whose row we actually touched).
        assert reclaimed == 0

        # Zero failure rows written.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert failure_inserts == [], (
            f"ECS agent must not write a failure row — got {failure_inserts}"
        )

        # Zero retry markers enqueued.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert marker_inserts == [], (
            f"ECS agent must not enqueue a retry marker — got {marker_inserts}"
        )

        # Zero ``UPDATE dispatcher.agents SET status='crashed'`` writes.
        crashed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and ("crashed" in e[1] or "daemon_restart_abandoned" in e[1])
        ]
        assert crashed_updates == [], (
            f"ECS agent row must stay status='running' — got {crashed_updates}"
        )

        # No ``agent_recovered_from_restart`` WARNING event.
        assert handler.events("agent_recovered_from_restart") == []

        # One ``agent_ecs_survived_daemon_restart`` INFO event with
        # the required observability fields.
        survived_events = handler.events("agent_ecs_survived_daemon_restart")
        assert len(survived_events) == 1
        record = survived_events[0]
        assert record.levelno == logging.INFO
        assert getattr(record, "agent_id") == "agent-ecs"
        assert getattr(record, "issue_number") == 2671
        assert getattr(record, "prior_phase") == "ralph"
        assert getattr(record, "agent_task_arn") == task_arn

    def test_subprocess_agent_still_abandoned(self, tmp_path: Path) -> None:
        """Explicit ``execution_mode='subprocess'`` preserves legacy behavior."""
        d, conn, handler = _make_daemon(tmp_path)

        conn.cursor_instance.fetchall_queue = [
            [("agent-sub", 2807, "ralph", "subprocess", None)],
        ]
        conn.cursor_instance.fetch_queue = [
            (0,),  # prior retry marker count
            ("[60,300,900]",),  # backoff schedule
        ]

        reclaimed = d.recover_abandoned_agents()
        assert reclaimed == 1

        # Existing subprocess path: failure row + terminal + retry marker.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1

        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1

        # Warning-level restart-abandoned event fired.
        assert len(handler.events("agent_recovered_from_restart")) == 1
        # No ECS survival event.
        assert handler.events("agent_ecs_survived_daemon_restart") == []

    def test_null_execution_mode_defaults_to_subprocess(self, tmp_path: Path) -> None:
        """Pre-#3091 rows with NULL ``execution_mode`` keep the legacy path.

        Migration 41 set ``execution_mode TEXT NOT NULL DEFAULT 'subprocess'``
        so in practice every row has a value, but the reclaim loop
        defensively coerces NULL to subprocess so a migration hiccup
        (or a hand-edited row) can't accidentally opt into the
        ECS-survives branch and leave an orphaned agent.
        """
        d, conn, handler = _make_daemon(tmp_path)

        conn.cursor_instance.fetchall_queue = [
            [("agent-old", 2500, "plan", None, None)],
        ]
        conn.cursor_instance.fetch_queue = [
            (0,),  # retry marker count
            ("[60,300,900]",),  # backoff
        ]

        reclaimed = d.recover_abandoned_agents()
        assert reclaimed == 1, (
            "NULL execution_mode must take the subprocess abandon path"
        )

        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        assert len(handler.events("agent_recovered_from_restart")) == 1
        assert handler.events("agent_ecs_survived_daemon_restart") == []

    def test_mixed_fleet_only_subprocess_abandoned(self, tmp_path: Path) -> None:
        """One subprocess + one ECS agent → only subprocess is abandoned."""
        d, conn, handler = _make_daemon(tmp_path)

        ecs_arn = (
            "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/"
            "abcdef0123456789abcdef0123456789"
        )
        conn.cursor_instance.fetchall_queue = [
            [
                ("agent-sub", 100, "ralph", "subprocess", None),
                ("agent-ecs", 200, "plan", "ecs", ecs_arn),
            ],
        ]
        # Only the subprocess agent hits fetch_queue — the ECS branch
        # is a pure ``continue`` before any write path runs.
        conn.cursor_instance.fetch_queue = [
            (0,),  # retry marker count for subprocess
            ("[60,300,900]",),  # backoff
        ]

        reclaimed = d.recover_abandoned_agents()
        assert reclaimed == 1, (
            "Mixed fleet: subprocess=1 abandoned, ecs=0 (deferred to reap tick)"
        )

        # Exactly one failure row (for the subprocess agent).
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        # The failure is for the subprocess agent, not the ECS one.
        assert "agent-sub" in str(failure_inserts[0][1])

        # Exactly one retry marker (for the subprocess agent).
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        assert "agent-sub" in str(marker_inserts[0][1])

        # One survival event (for the ECS agent).
        survived_events = handler.events("agent_ecs_survived_daemon_restart")
        assert len(survived_events) == 1
        assert getattr(survived_events[0], "agent_id") == "agent-ecs"
        assert getattr(survived_events[0], "agent_task_arn") == ecs_arn

        # One abandonment event (for the subprocess agent).
        abandoned_events = handler.events("agent_recovered_from_restart")
        assert len(abandoned_events) == 1
        assert getattr(abandoned_events[0], "agent_id") == "agent-sub"

    def test_select_includes_execution_mode_and_task_arn(self, tmp_path: Path) -> None:
        """The SELECT must include the two new columns used for branching."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        d.recover_abandoned_agents()

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT" in e[0] and "dispatcher.agents" in e[0]
        ]
        assert selects
        sql, _params = selects[0]
        assert "execution_mode" in sql, (
            "SELECT must return execution_mode so the loop can branch — "
            f"actual: {sql!r}"
        )
        assert "agent_task_arn" in sql, (
            "SELECT must return agent_task_arn for observability logging — "
            f"actual: {sql!r}"
        )


# --------------------------------------------------------------------------
# Bug #3152 — _reap_completed_agent_tasks noop on surviving RUNNING ECS task
# --------------------------------------------------------------------------


class TestReapNoopOnSurvivingEcsAgent:
    """After startup survival, the reap tick must observe RUNNING → noop.

    This is the post-#3152 end-to-end contract: fresh daemon skipped
    the restart-abandon path for the ECS row in
    :meth:`recover_abandoned_agents`, and the next scheduler tick
    picks up the still-alive ARN via ``ecs:DescribeTasks`` and
    takes no action. Row stays ``status='running'``.

    The reap-path test suite
    (``test_daemon_reap_completed_agent_tasks.py``) covers the full
    lifecycle matrix. This test fixes a specific regression: the reap
    tick must not crash or mark terminal when given an ECS agent
    whose row is still ``running`` and whose ECS task is still
    ``RUNNING`` — which is the exact state a survived-restart ECS
    agent presents to the very next tick.
    """

    def test_reap_noop_on_running_task(self, tmp_path: Path) -> None:
        import threading
        from unittest.mock import MagicMock

        # Build a minimal daemon with the same wiring
        # ``test_daemon_reap_completed_agent_tasks.py`` uses.
        d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
        d._thread_state = threading.local()  # type: ignore[attr-defined]

        class _ReapCursor:
            def __init__(self, rows: list[Any]) -> None:
                self.executed: list[tuple[str, Any]] = []
                self._rows = rows

            def __enter__(self) -> _ReapCursor:
                return self

            def __exit__(self, *_exc: Any) -> None:
                return None

            def execute(self, sql: str, params: Any = None) -> None:
                self.executed.append((sql, params))

            def fetchall(self) -> list[Any]:
                return self._rows

            def fetchone(self) -> Any:
                return None

        arn = (
            "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/"
            "eb57861c531e41359d99e18c95d86e0e"
        )
        cur = _ReapCursor(rows=[("agent-ecs", 2671, arn, "ralph", "running")])

        class _Conn:
            def __init__(self, c: _ReapCursor) -> None:
                self._c = c
                self.committed = 0
                self.rolled_back = 0

            def cursor(self) -> _ReapCursor:
                return self._c

            def commit(self) -> None:
                self.committed += 1

            def rollback(self) -> None:
                self.rolled_back += 1

        conn = _Conn(cur)
        d._main_conn = conn  # type: ignore[attr-defined]
        d._cfg = MagicMock(  # type: ignore[attr-defined]
            aws_region="us-west-2",
            ecs_cluster_arn=(
                "arn:aws:ecs:us-west-2:155326049300:cluster/judgemind-dev"
            ),
        )
        d._log = logging.getLogger(  # type: ignore[attr-defined]
            f"test.daemon_reap_survive.{id(tmp_path)}"
        )
        d._run_id = "new-run-after-restart"  # type: ignore[attr-defined]

        # ECS DescribeTasks returns the still-alive task.
        ecs_client = MagicMock()
        ecs_client.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": arn,
                    "lastStatus": "RUNNING",
                }
            ],
        }
        d._ecs_client = ecs_client  # type: ignore[attr-defined]

        summary = d._reap_completed_agent_tasks()

        # RUNNING → noop: increment still_running, nothing else.
        assert summary["active"] == 1
        assert summary["still_running"] == 1
        assert summary["reaped_success"] == 0
        assert summary["reaped_failure"] == 0

        # No terminal UPDATE was issued against the row.
        agent_updates = [
            e
            for e in cur.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and ("crashed" in str(e[1]) or "succeeded" in str(e[1]))
        ]
        assert agent_updates == [], (
            f"RUNNING ECS task must not be marked terminal by reap — {agent_updates}"
        )


# --------------------------------------------------------------------------
# Bug B — per-phase stuck_timeout thresholds
# --------------------------------------------------------------------------


class TestPerPhaseStuckTimeout:
    """Per-phase thresholds prevent over-aggressive stuck_timeout firing.

    The 2026-04-19 cascade saw stuck_timeout fire on a 2.5-minute
    ralph and a 90-second plan because the old global 30-minute
    threshold was compared against a stale ``phase_transitions.MAX(ts)``
    carried across retries. Even with retry-reset writing a fresh
    transitions row (#2872 Bug B root cause), a single threshold is
    too coarse — ralph legitimately runs 3-90 minutes, so the table
    now lets each phase declare its own window.
    """

    def test_ralph_below_threshold_not_flagged(self, tmp_path: Path) -> None:
        """A 2.5-minute ralph is not stuck (threshold is 90 min)."""
        d, conn, _handler = _make_daemon(tmp_path)
        # SELECT returns (agent_id, issue_number, phase, elapsed).
        # Post-#2927 the #2903 ``kind`` column was dropped.
        conn.cursor_instance.fetchall_queue = [
            [("agent-1", 42, "ralph", 150.0)],  # 2.5 min elapsed
        ]
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
        ]
        assert d._check_stuck_agents() == 0

    def test_plan_below_threshold_not_flagged(self, tmp_path: Path) -> None:
        """A 90-second plan is not stuck (threshold is 2.5 hr, #2885)."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [("agent-1", 42, "plan", 90.0)],
        ]
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
        ]
        assert d._check_stuck_agents() == 0

    def test_ralph_over_threshold_flagged(self, tmp_path: Path) -> None:
        """A 16-hour ralph IS stuck (threshold is 15 hr / 54000s, #2885)."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [("agent-1", 42, "ralph", 57600.0)],  # 16 hr
        ]
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
            (42,),  # failure_id RETURNING from _write_failure
            None,  # _has_prior_stuck_timeout_in_window — no prior
            (0,),  # prior retry marker count
            ("[60,300,900]",),  # backoff
        ]
        assert d._check_stuck_agents() == 1
        detected = handler.events("failure_detected")
        assert detected
        # Structured log includes the per-phase threshold for ops debug.
        assert getattr(detected[0], "threshold_seconds", None) == 54000

    def test_config_override_wins_over_default(self, tmp_path: Path) -> None:
        """Operator override in ``stuck_timeout_s_by_phase`` takes precedence."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Elapsed 400s — under the default 5min claiming threshold, but
        # operator set claiming override to 300s. Should fire.
        conn.cursor_instance.fetchall_queue = [
            [("agent-1", 42, "claiming", 400.0)],
        ]
        conn.cursor_instance.fetch_queue = [
            ('{"claiming": 300}',),  # operator override
            (42,),  # failure_id RETURNING from _write_failure
            None,  # _has_prior_stuck_timeout_in_window — no prior
            (0,),  # prior marker count
            ("[60,300,900]",),
        ]
        assert d._check_stuck_agents() == 1

    def test_unknown_phase_falls_back_to_global(self, tmp_path: Path) -> None:
        """Phase not in the table falls back to STUCK_TIMEOUT_SECONDS (30 min)."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Novel phase, elapsed 31 min — over the 30 min default fallback.
        conn.cursor_instance.fetchall_queue = [
            [("agent-1", 42, "novel_phase", 31 * 60.0)],
        ]
        conn.cursor_instance.fetch_queue = [
            None,  # no override
            (42,),  # failure_id RETURNING from _write_failure
            None,  # _has_prior_stuck_timeout_in_window — no prior
            (0,),  # prior marker count
            ("[60,300,900]",),
        ]
        assert d._check_stuck_agents() == 1

    def test_stuck_timeout_for_phase_table_values(self) -> None:
        """Module-level defaults match the documented spec (#2872 + #2885 10× bump)."""
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["ralph"] == 54000  # 15 hr
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["plan"] == 9000  # 2.5 hr
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["summary"] == 6000
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["verify"] == 3000
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["retro"] == 3000
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["fix_ci"] == 18000
        # Non-LLM phases keep original tight windows — external-system-bound.
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["claiming"] == 5 * 60
        # Restart-recovery "terminal" phases have a tight window so a
        # daemon that crashes mid-reclaim is picked up quickly.
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["daemon_restart_abandoned"] == 60
        # Route-stub terminal phases also get a tight window (#3300).
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["agent_runner_route_stub"] == 60


# --------------------------------------------------------------------------
# Issue #2885 — 10× stuck_timeout + max_turns bump.
# --------------------------------------------------------------------------


class TestIssue2885TenTimesBump:
    """Regressions for the #2885 10× bump.

    Overnight 2026-04-20 observed two distinct failure modes:

    1. Ralph race. Agent ``821e96ee`` on #2565 ran ralph 5834s. The
       supervisor flagged it stuck at 5419s (old 90 min threshold, 19s
       of slack), fired a retry. Ralph exited cleanly 415s later but
       the retry had already taken over — ``phase_output_missing`` +
       failed agent despite ralph actually succeeding.
    2. Plan ``error_max_turns``. Plan agents hit 51/50 turns on complex
       scraper issues (#2564, #2565), burning ~$3 of opus with zero
       output. Two agents tripped this within 5 min 24s on 2026-04-20.

    Operator directive: "stop being parsimonious on these limits.
    Cost is not the constraint; success is." Every LLM-bearing phase
    gets a 10× headroom bump so a normally-progressing subprocess
    never races against the supervisor's stuck-declaration.
    """

    def test_max_turns_all_bumped_to_ten_times(self) -> None:
        """Every phase in PHASE_MAX_TURNS is at least 10× the original calibration."""
        # Original values from #2787 Phase 3B + #2798 Phase 3E.
        original = {
            "plan": 50,
            "ralph": 500,
            "summary": 30,
            "fix-ci": 100,
            "verify": 50,
            "retro": 30,
        }
        for phase, old in original.items():
            assert phase in daemon.PHASE_MAX_TURNS, (
                f"phase {phase!r} missing from PHASE_MAX_TURNS"
            )
            assert daemon.PHASE_MAX_TURNS[phase] >= old * 10, (
                f"phase {phase!r} max_turns not 10×-bumped: got "
                f"{daemon.PHASE_MAX_TURNS[phase]}, expected >= {old * 10}"
            )

    def test_max_turns_exact_issue_values(self) -> None:
        """Exact values called out in #2885."""
        assert daemon.PHASE_MAX_TURNS["plan"] == 500
        assert daemon.PHASE_MAX_TURNS["ralph"] == 5000
        assert daemon.PHASE_MAX_TURNS["summary"] == 300
        assert daemon.PHASE_MAX_TURNS["verify"] == 500
        assert daemon.PHASE_MAX_TURNS["retro"] == 300
        # fix-ci's old value was 100 (not 50 as misremembered in the
        # issue body); 10× is 1000.
        assert daemon.PHASE_MAX_TURNS["fix-ci"] == 1000

    def test_stuck_timeout_llm_phases_match_issue_targets(self) -> None:
        """Every LLM-bearing phase matches the explicit target values in #2885.

        The issue calls out exact target values rather than a blanket
        10× formula (because the pre-#2885 ralph threshold of 5400s
        was already under the rest of the module defaults — the table
        was inconsistent). These values are what #2885 explicitly
        requested; treating them as the authoritative spec here
        guards against "fix the parsimony, lose the parsimony, fix
        again" drift.
        """
        issue_targets = {
            "planning": 9000,
            "plan": 9000,
            "ralph": 54000,
            "summary": 6000,
            "verify": 3000,
            "fix_ci": 18000,
            "retro": 3000,
        }
        for phase, target in issue_targets.items():
            assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE[phase] == target, (
                f"phase {phase!r} stuck_timeout not at issue #2885 target: "
                f"got {daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE[phase]}, "
                f"expected {target}"
            )

    def test_ralph_race_regression(self, tmp_path: Path) -> None:
        """Agent ``821e96ee`` at 5419s would NOT be flagged under the new threshold.

        Concrete reproduction of the overnight race. Pre-#2885 ralph
        threshold was 5400s, so 5419s tripped the supervisor even
        though the subprocess was 15s from clean exit. The new 54000s
        threshold gives ~15 hr of headroom — enough that a
        normally-progressing ralph can NEVER race against the timer.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [("agent-821e96ee", 2565, "ralph", 5419.0, "task")],
        ]
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
        ]
        # Pre-fix: 1. Post-fix: 0.
        assert d._check_stuck_agents() == 0


# --------------------------------------------------------------------------
# Bug E — phase_outputs retry capability (attempt column)
# --------------------------------------------------------------------------


class TestPhaseOutputsRetry:
    """Retry-capable ``phase_outputs`` schema preserves second-run data.

    Before #2872 migration 30, the ``(agent_id, phase)`` unique index
    rejected the second INSERT on a retry path — the daemon silently
    rolled back and the admin page lost the output. Now the INSERT
    includes ``attempt`` (derived from ``retries_used``) and the
    index is ``(agent_id, phase, attempt)`` so retries preserve
    history.
    """

    def test_persist_reads_retries_used_and_writes_attempt(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # retries_used=2 means this is the 3rd attempt.
        conn.cursor_instance.fetch_queue = [(2,)]
        d._persist_phase_output("agent-x", "plan", {"go": True})

        # Find the phase_outputs INSERT and assert attempt parameter.
        insert_outputs = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert len(insert_outputs) == 1
        sql, params = insert_outputs[0]
        # Params: (agent_id, phase, output_json, log_text, attempt).
        assert params[0] == "agent-x"
        assert params[1] == "plan"
        assert params[4] == 2
        # SQL should target the new attempt-aware unique constraint.
        assert "ON CONFLICT (agent_id, phase, attempt)" in sql

    def test_persist_defaults_attempt_to_zero_when_row_missing(
        self, tmp_path: Path
    ) -> None:
        """Missing ``dispatcher.agents`` row → attempt falls back to 0.

        Defensive: ``_current_attempt_for`` must not blow up the INSERT
        on a read failure or missing row. Losing a phase record is
        worse than using a potentially stale attempt number.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]  # agent row missing
        d._persist_phase_output("agent-y", "plan", {"go": True})

        insert_outputs = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_outputs" in e[0]
        ]
        assert insert_outputs
        assert insert_outputs[0][1][4] == 0

    def test_current_attempt_for_handles_db_error(self, tmp_path: Path) -> None:
        """DB error in the retries_used read → attempt=0 + rollback."""
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        assert d._current_attempt_for("agent-z") == 0
        assert conn.rollbacks >= 1


# --------------------------------------------------------------------------
# Bug F — worker thread respects external terminal writes
# --------------------------------------------------------------------------


class TestExternalTerminalAbort:
    """Worker aborts at the next phase boundary on external terminal writes.

    Before #2872 Bug F, the diagnoser could write
    ``dispatcher.agents.status='failed'`` and the worker thread kept
    running phases — producing the zombie state observed 2026-04-19
    20:09:44 → 20:12:16. ``_check_killswitch_and_abort`` now observes
    the row status and aborts.
    """

    def test_aborts_on_external_failed_status(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # Supervisor/diagnoser wrote 'failed' externally.
        conn.cursor_instance.fetch_queue = [("failed",)]

        aborted = d._check_killswitch_and_abort("agent-zombie", "plan")
        assert aborted is True

        # Log event emitted so CloudWatch tracks the abort.
        events = handler.events("orchestration_terminated_externally")
        assert len(events) == 1
        assert getattr(events[0], "observed_status", None) == "failed"

        # Does NOT re-mark terminal — whoever wrote 'failed' owns the row.
        mark_calls = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "paused_by_killswitch" in str(e[1])
        ]
        assert not mark_calls, f"unexpected killswitch mark calls: {mark_calls}"

    def test_aborts_on_each_terminal_status(self, tmp_path: Path) -> None:
        """All statuses in TERMINAL_AGENT_STATUSES trigger the abort."""
        for terminal in daemon.TERMINAL_AGENT_STATUSES:
            d, conn, _handler = _make_daemon(tmp_path)
            conn.cursor_instance.fetch_queue = [(terminal,)]
            assert d._check_killswitch_and_abort("agent-1", "plan") is True, (
                f"failed to abort on status={terminal}"
            )

    def test_does_not_abort_on_running(self, tmp_path: Path) -> None:
        """Running status is normal — no abort signal."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("running",)]
        # Killswitch event NOT set either.
        assert d._check_killswitch_and_abort("agent-1", "plan") is False

    def test_does_not_abort_on_retrying(self, tmp_path: Path) -> None:
        """Retrying status is transitional — no abort signal."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("retrying",)]
        assert d._check_killswitch_and_abort("agent-1", "plan") is False

    def test_external_terminal_precedence_over_killswitch(self, tmp_path: Path) -> None:
        """External terminal wins over killswitch (doesn't overwrite row)."""
        d, conn, handler = _make_daemon(tmp_path)
        d._pause_requested.set()  # killswitch engaged
        conn.cursor_instance.fetch_queue = [("failed",)]  # + external terminal

        aborted = d._check_killswitch_and_abort("agent-1", "plan")
        assert aborted is True

        # Log event is external-terminal, not orchestration_paused.
        assert handler.events("orchestration_terminated_externally")
        assert not handler.events("orchestration_paused")

    def test_observe_external_terminal_returns_status(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("crashed",)]
        assert d._observe_external_terminal("agent-1") == "crashed"

    def test_observe_external_terminal_returns_none_on_nonterminal(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("running",)]
        assert d._observe_external_terminal("agent-1") is None

    def test_observe_external_terminal_returns_none_on_missing(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._observe_external_terminal("agent-1") is None

    def test_observe_external_terminal_returns_none_on_db_error(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        def boom(sql: str, params: Any = None) -> None:
            raise RuntimeError("db lost")

        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        assert d._observe_external_terminal("agent-1") is None


# --------------------------------------------------------------------------
# Retry state reset writes phase_transitions rows (Bugs A+B root cause)
# --------------------------------------------------------------------------


class TestRetryResetWritesTransition:
    """Retry paths write a fresh ``phase_transitions`` row.

    Before #2872, ``_process_retry_markers`` and
    ``_resume_retrying_agent`` reset ``dispatcher.agents.status`` but
    didn't write a new transitions row. The supervisor's stuck_timeout
    MAX(ts) therefore continued to see the stale pre-retry timestamp
    and fired immediately again — the compounding core of the
    cascading retry loop.
    """

    def test_process_retry_markers_writes_retry_reset_transition(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # One due retry marker.
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    1,  # marker_id
                    "agent-retry",
                    daemon.FAILURE_CATEGORY_STUCK_TIMEOUT,
                    1,  # attempt
                    "/tmp/worktree",  # worktree_path
                    42,  # issue_number
                ),
            ],
        ]

        # Mock _drop_worktree_best_effort to a no-op.
        def noop_drop(self, path: str) -> None:  # noqa: ARG001
            return None

        d._drop_worktree_best_effort = noop_drop.__get__(d)  # type: ignore[method-assign]

        processed = d._process_retry_markers()
        assert processed == 1

        # The retry path wrote a phase_transitions row with
        # phase='retry_reset'.
        transition_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_transitions" in e[0]
        ]
        assert transition_inserts
        assert transition_inserts[0][1] == ("agent-retry", "retry_reset")

    def test_resume_retrying_agent_writes_resumed_transition(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)

        # Step 1: SELECT the retrying agent.
        conn.cursor_instance.fetch_queue = [
            ("agent-resume", 42),  # (agent_id, issue_number)
        ]

        # Stub out the parts of resume we don't want to exercise here.
        # _create_worktree returns a Path (not exercised by the code
        # under test), and _run_orchestration_phases is a no-op.
        d._create_worktree = MagicMock(return_value=tmp_path)  # type: ignore[method-assign]
        d._run_orchestration_phases = MagicMock()  # type: ignore[method-assign]

        resumed = d._resume_retrying_agent()
        assert resumed is True

        # Among the UPDATE + INSERT calls, one INSERT into
        # phase_transitions with phase='resumed' must exist.
        transition_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_transitions" in e[0]
        ]
        assert transition_inserts
        assert transition_inserts[0][1] == ("agent-resume", "resumed")


# --------------------------------------------------------------------------
# Bug D — ensure_required_labels creates status/needs-human idempotently
# --------------------------------------------------------------------------


class TestEnsureRequiredLabels:
    """Startup helper creates status/needs-human so diagnoser escalate works."""

    def test_issues_gh_label_create_force(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        attempted = d.ensure_required_labels()
        assert attempted >= 1
        # status/needs-human is in the essential set.
        assert any("status/needs-human" in c for c in calls)
        # --force is passed so re-runs are idempotent (gh 'create' is
        # otherwise non-idempotent — it errors on an existing label).
        assert any("--force" in c for c in calls)
        # gh label create is the subcommand.
        assert any("label" in c and "create" in c for c in calls)

    def test_label_failure_non_fatal(self, tmp_path: Path, monkeypatch: Any) -> None:
        """A gh failure logs but does not raise — startup must not block."""
        d, _conn, _handler = _make_daemon(tmp_path)

        def boom(cmd: list[str], **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd, timeout=10)

        monkeypatch.setattr(subprocess, "run", boom)

        # Should not raise.
        d.ensure_required_labels()


# --------------------------------------------------------------------------
# Tier-1 retry classification — daemon_restart_abandoned is in the set
# --------------------------------------------------------------------------


class TestAutoRetryCategoriesIncludesRestartAbandoned:
    """``daemon_restart_abandoned`` is in AUTO_RETRY_CATEGORIES.

    Without this, ``_create_retry_marker`` would reject the enqueue
    with ``retry_marker_skipped`` and the recovered agent would sit
    idle forever.
    """

    def test_restart_abandoned_in_auto_retry(self) -> None:
        assert (
            daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED
            in daemon.AUTO_RETRY_CATEGORIES
        )


# --------------------------------------------------------------------------
# Issue #2925 — terminal-and-reclaim for infra-preemption retry markers
# --------------------------------------------------------------------------


class TestProcessRetryMarkersInfraPreemption:
    """Infra-preemption markers take the terminal-and-reclaim path.

    Before #2925, ``_process_retry_markers`` reset infra-preemption markers
    to ``status='retrying' phase='claiming'`` — leaving a zombie row in
    ``dispatcher.agents`` with ``ended_at=NULL`` that blocked the partial
    UNIQUE INDEX and prevented fresh claims. The fix instead marks the old
    row terminal (``status='failed'``, ``phase=reason``, ``ended_at=now()``)
    and adds ``agent/ready`` back to the issue so the scheduler re-discovers
    it.
    """

    def _make_infra_marker_row(
        self,
        tmp_path: Path,
        reason: str,
        issue_number: int = 42,
    ) -> tuple[Any, _FakeConnection, _CapturingLogHandler]:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    1,  # marker_id
                    "agent-infra-preempted",
                    reason,
                    1,  # attempt
                    str(tmp_path / "worktrees" / "agent-infra"),  # worktree_path
                    issue_number,
                ),
            ],
        ]
        return d, conn, handler

    def test_restart_abandoned_terminates_old_and_unblocks_claim(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``daemon_restart_abandoned`` marks the row failed + adds agent/ready.

        Verifies:
        - ``_mark_agent_terminal`` called with ``status='failed'``,
          ``phase='daemon_restart_abandoned'``, ``issue_number=42``.
        - ``agent/ready`` label added via ``_gh_issue_add_labels``.
        - Marker resolved via ``UPDATE dispatcher.retry_markers SET resolved_at``.
        - No ``status='retrying'`` UPDATE emitted.
        - No ``phase_transitions ('retry_reset')`` INSERT emitted.
        - ``daemon.retry_terminal_and_reclaim`` log event emitted.
        """
        d, conn, handler = self._make_infra_marker_row(
            tmp_path, daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED
        )
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        gh_calls: list[tuple[int, list[str]]] = []

        def fake_gh_add(issue_num: int, labels: list[str]) -> None:
            gh_calls.append((issue_num, labels))

        monkeypatch.setattr(d, "_gh_issue_add_labels", fake_gh_add)

        processed = d._process_retry_markers()
        assert processed == 1

        # agent/ready added.
        assert gh_calls == [(42, ["agent/ready"])], (
            f"expected [(42, ['agent/ready'])], got {gh_calls}"
        )

        # No retrying-reset UPDATE.
        retrying_resets = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "status = 'retrying'" in e[0]
        ]
        assert retrying_resets == [], (
            f"infra-preemption path must not emit retrying reset; got {retrying_resets}"
        )

        # No retry_reset phase_transition.
        retry_reset_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.phase_transitions" in e[0]
            and e[1] is not None
            and "retry_reset" in str(e[1])
        ]
        assert retry_reset_inserts == [], (
            f"infra-preemption path must not insert retry_reset transition; got {retry_reset_inserts}"
        )

        # Marker resolved.
        resolves = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.retry_markers" in e[0] and "resolved_at" in e[0]
        ]
        assert resolves, "retry marker must be resolved"

        # Terminal-and-reclaim log event emitted.
        reclaim_events = handler.events("retry_terminal_and_reclaim")
        assert len(reclaim_events) == 1
        assert getattr(reclaim_events[0], "reason", None) == (
            daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED
        )

        # No retry_processed event (that's for the budgeted path).
        assert handler.events("retry_processed") == []

    def test_paused_by_killswitch_takes_same_terminal_path(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``paused_by_killswitch`` follows the same terminal-and-reclaim flow."""
        d, conn, handler = self._make_infra_marker_row(
            tmp_path, daemon.FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH
        )
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)

        gh_calls: list[tuple[int, list[str]]] = []
        monkeypatch.setattr(
            d, "_gh_issue_add_labels", lambda n, labels: gh_calls.append((n, labels))
        )

        processed = d._process_retry_markers()
        assert processed == 1

        assert gh_calls == [(42, ["agent/ready"])]

        retrying_resets = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0] and "status = 'retrying'" in e[0]
        ]
        assert retrying_resets == []

        reclaim_events = handler.events("retry_terminal_and_reclaim")
        assert len(reclaim_events) == 1
        assert getattr(reclaim_events[0], "reason", None) == (
            daemon.FAILURE_CATEGORY_PAUSED_BY_KILLSWITCH
        )

    def test_mark_agent_terminal_called_with_correct_args(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """``_mark_agent_terminal`` receives status='failed', phase=reason, issue_number.

        Integration-style: verify the _mark_agent_terminal call shape
        mirrors the ``recover_abandoned_agents`` precedent (line 3660).
        """
        d, conn, handler = self._make_infra_marker_row(
            tmp_path,
            daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED,
            issue_number=2925,
        )
        monkeypatch.setattr(d, "_drop_worktree_best_effort", lambda _p: True)
        monkeypatch.setattr(d, "_gh_issue_add_labels", lambda n, labels: None)

        terminal_calls: list[dict[str, Any]] = []

        original_mark = d._mark_agent_terminal

        def capturing_mark(
            agent_id: str,
            status: str,
            phase: str,
            exit_code: int | None = None,
            pr_number: int | None = None,
            issue_number: int | None = None,
        ) -> None:
            terminal_calls.append(
                {
                    "agent_id": agent_id,
                    "status": status,
                    "phase": phase,
                    "exit_code": exit_code,
                    "issue_number": issue_number,
                }
            )
            original_mark(
                agent_id,
                status=status,
                phase=phase,
                exit_code=exit_code,
                issue_number=issue_number,
            )

        monkeypatch.setattr(d, "_mark_agent_terminal", capturing_mark)

        d._process_retry_markers()

        assert len(terminal_calls) == 1
        call = terminal_calls[0]
        assert call["status"] == "failed"
        assert call["phase"] == daemon.FAILURE_CATEGORY_DAEMON_RESTART_ABANDONED
        assert call["exit_code"] is None
        assert call["issue_number"] == 2925


# --------------------------------------------------------------------------
# Issue #2925 — _drop_worktree_best_effort missing path guard
# --------------------------------------------------------------------------


class TestDropWorktreeBestEffortMissingPath:
    """When the worktree path doesn't exist, no subprocess fires.

    Before #2925, ``_drop_worktree_best_effort`` always tried
    ``cleanup_worktree.sh`` and/or ``git worktree remove`` even when the
    path was already gone — emitting the noisy ``worktree_remove_nonzero``
    WARNING on every daemon restart. The fix returns True immediately
    when ``Path(worktree_path).exists()`` is False and logs a lighter
    ``worktree_already_gone`` INFO event.
    """

    def test_missing_path_logs_already_gone_no_subprocess(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        subprocess_calls: list[Any] = []

        def fake_run(cmd: Any, **_kwargs: Any) -> Any:
            subprocess_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Stub Path.exists to return False for any path.
        monkeypatch.setattr(
            "dispatcher.daemon.Path",
            lambda p: type("_FakePath", (), {"exists": lambda self: False})(),
        )

        result = d._drop_worktree_best_effort("/nonexistent/worktree/agent-abc")

        assert result is True, "should return True (path already gone = success)"
        assert subprocess_calls == [], (
            f"no subprocess should fire when path is missing; got {subprocess_calls}"
        )

        events = handler.events("worktree_already_gone")
        assert len(events) == 1
        assert getattr(events[0], "worktree_path", None) == (
            "/nonexistent/worktree/agent-abc"
        )
        # INFO level, not WARNING.
        assert events[0].levelno == logging.INFO, (
            f"expected INFO, got {logging.getLevelName(events[0].levelno)}"
        )
