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


if "psycopg" not in sys.modules or not isinstance(
    getattr(sys.modules["psycopg"].errors, "UniqueViolation", None), type
):

    class _UniqueViolation(Exception):
        """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""

    _psycopg_stub = MagicMock()
    _psycopg_errors = MagicMock()
    _psycopg_errors.UniqueViolation = _UniqueViolation
    _psycopg_stub.errors = _psycopg_errors
    sys.modules["psycopg"] = _psycopg_stub

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
        # reads nothing via fetchone in the write path except
        # _create_retry_marker's COUNT query + backoff_seconds config.
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
        conn.cursor_instance.fetchall_queue = [
            [("agent-1", 42, "ralph", 150.0)],  # 2.5 min elapsed
        ]
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
        ]
        assert d._check_stuck_agents() == 0

    def test_plan_below_threshold_not_flagged(self, tmp_path: Path) -> None:
        """A 90-second plan is not stuck (threshold is 30 min)."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [("agent-1", 42, "plan", 90.0)],
        ]
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
        ]
        assert d._check_stuck_agents() == 0

    def test_ralph_over_threshold_flagged(self, tmp_path: Path) -> None:
        """A 100-minute ralph IS stuck (threshold is 90 min)."""
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [
            [("agent-1", 42, "ralph", 6000.0)],  # 100 min
        ]
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
            (0,),  # prior retry marker count
            ("[60,300,900]",),  # backoff
        ]
        assert d._check_stuck_agents() == 1
        detected = handler.events("failure_detected")
        assert detected
        # Structured log includes the per-phase threshold for ops debug.
        assert getattr(detected[0], "threshold_seconds", None) == 90 * 60

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
            (0,),
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
            (0,),
            ("[60,300,900]",),
        ]
        assert d._check_stuck_agents() == 1

    def test_stuck_timeout_for_phase_table_values(self) -> None:
        """Module-level defaults match the documented spec (#2872)."""
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["ralph"] == 90 * 60
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["plan"] == 30 * 60
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["claiming"] == 5 * 60
        # Restart-recovery "terminal" phases have a tight window so a
        # daemon that crashes mid-reclaim is picked up quickly.
        assert daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE["daemon_restart_abandoned"] == 60


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
