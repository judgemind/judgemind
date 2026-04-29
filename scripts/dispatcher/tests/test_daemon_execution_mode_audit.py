"""Unit tests for the #3158 ``execution_mode`` awareness audit fixes.

Issue #3158. The audit
(``docs/investigations/dispatcher-execution-mode-audit-2026-04.md``)
classified every ``dispatcher.agents`` read/write in
``scripts/dispatcher/daemon.py`` and fixed three sites that silently
mishandled ECS-mode agents:

* **Fix 1 — ``_check_stuck_agents``** — SELECT filter now excludes
  ``execution_mode = 'ecs'`` rows so the per-phase stuck timer can
  never flip an ECS agent to ``crashed`` (which pre-fix would enqueue
  a retry marker and fork the agent to subprocess via
  ``_resume_retrying_agent``).
* **Fix 2 — ``_resume_retrying_agent``** — SELECT filter now excludes
  ``execution_mode = 'ecs'`` rows so a retrying ECS agent is never
  silently relaunched as a subprocess. Paired with a WARNING event
  when an ECS-retrying row is observed (operator signal until
  FOLLOW-4 wires ECS-native retry).
* **Fix 3 — ``_handle_force_stop`` per-agent** — signal-delivery
  branches on ``execution_mode``: subprocess → SIGKILL pid,
  ECS → ``ecs:StopTask(agent_task_arn)``. Pre-fix the SIGKILL
  no-oped for ECS agents because ``pid`` was NULL on the daemon host.

All three fixes are strictly additive — subprocess-mode behaviour is
unchanged. Tests below pin both the subprocess path (must not regress)
and the ECS path (new behaviour).
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402 — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes (same shape as test_daemon_restart_cascade.py)
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
    ecs_cluster_arn: str | None = None,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.execution_mode_audit.{id(tmp_path)}")
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
        ecs_cluster_arn=ecs_cluster_arn,
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# Fix 1 — _check_stuck_agents handles BOTH execution modes (#3656)
# --------------------------------------------------------------------------


class TestCheckStuckAgentsExecutionModeFilter:
    """The supervisor stuck-timeout scan must reap BOTH execution modes.

    History:

    * Pre-#3158, the SELECT returned every ``status='running'`` row and
      applied the per-phase threshold uniformly. For a long ECS ralph
      exceeding 15h the scan would flip the ECS row to ``crashed`` and
      enqueue a retry marker — which (absent the #3158 fix in
      :meth:`_resume_retrying_agent`) would silently fork the agent to
      subprocess mode on the next scheduler tick.
    * #3158 (2026-04-…) added
      ``COALESCE(a.execution_mode, 'subprocess') <> 'ecs'`` so ECS rows
      never reached the per-phase timer. The premise: a hung ECS task
      would surface as a STOPPED transition reaped by
      :meth:`_reap_completed_agent_tasks`.
    * #3656 (2026-04-27) — the premise was wrong. ``bash`` keeps
      running while a child ``git push`` blocks indefinitely, so ECS
      never STOPs the task. Observed for 16+ minutes on agent
      ``2ff6e282`` (#3608) before manual ``aws ecs stop-task``. The
      fix removes the exclusion: the SELECT returns every
      ``status='running'`` row plus ``execution_mode`` and
      ``agent_task_arn``, and the Python loop branches by mode —
      subprocess takes the legacy ``stuck_timeout`` + retry-marker
      path; ECS takes a new ``ecs:StopTask`` + ``agent_silent_hang``
      → diagnoser path. See
      :class:`~dispatcher.tests.test_daemon_stuck_check_ecs_silent_hang.TestEcsSilentHangReaper`
      for the ECS-branch coverage.
    """

    def test_select_no_longer_filters_execution_mode_ecs(self, tmp_path: Path) -> None:
        """The SELECT carries no ``<> 'ecs'`` clause (#3656).

        Asserts both directions of the change at once:

        * The legacy filter substring is absent — confirms #3158's
          exclusion was actually removed.
        * The new SELECT pulls ``execution_mode`` and
          ``agent_task_arn`` so the Python loop has the data it needs
          to dispatch the ECS branch.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]

        d._check_stuck_agents()

        # The first SELECT is the candidate scan; later cursor.execute
        # calls belong to the per-phase config read which may happen
        # in the same tick.
        selects = [
            e
            for e in conn.cursor_instance.executed
            if "FROM dispatcher.agents a" in e[0]
        ]
        assert selects, "expected _check_stuck_agents to issue the SELECT"
        sql, _params = selects[0]
        # #3656 — the legacy #3158 filter is gone.
        assert "<> 'ecs'" not in sql, (
            "#3656 removed the execution_mode='ecs' exclusion; the SELECT "
            "should now reap ECS rows too. Actual SQL: " + sql
        )
        # The mode + arn columns are present so the Python loop can
        # dispatch ECS rows to ``ecs:StopTask`` + ``_handle_agent_failure``.
        assert "execution_mode" in sql, (
            "#3656 — SELECT must return execution_mode for the Python "
            "branch dispatch. Actual SQL: " + sql
        )
        assert "agent_task_arn" in sql, (
            "#3656 — SELECT must return agent_task_arn so _force_stop_ecs_task "
            "can issue the StopTask call. Actual SQL: " + sql
        )

    def test_subprocess_agent_still_flagged_on_timeout(self, tmp_path: Path) -> None:
        """A subprocess-mode agent stuck past its threshold still flips crashed."""
        d, conn, handler = _make_daemon(tmp_path)
        # Override both config reads so we fall back to module defaults
        # (ralph stuck=54000s, ralph silent_hang=5400s).
        d._read_stuck_timeout_overrides = lambda: {}  # type: ignore[assignment]
        d._read_silent_hang_timeout_overrides = lambda: {}  # type: ignore[assignment]
        # Post-#3731 row shape: (agent_id, issue_number, phase,
        # silent_seconds, total_runtime_seconds, execution_mode,
        # agent_task_arn). Set silent=1800 (under 5400 silent
        # threshold) and total=60000 (over 54000 stuck threshold) so
        # ONLY total-runtime trips → stuck_timeout category preserved.
        conn.cursor_instance.fetchall_queue = [
            [("agent-sub", 2807, "ralph", 1800.0, 60000.0, "subprocess", None)],
        ]
        # Per _flag_stuck_agents path: failure_id RETURNING from
        # _write_failure, prior stuck check (no prior), retry marker
        # COUNT, backoff config. Override reads are stubbed via the
        # method replacements above so no fetchone for those.
        conn.cursor_instance.fetch_queue = [
            (42,),  # failure_id RETURNING from _write_failure
            None,  # _has_prior_stuck_timeout_in_window — no prior
            (0,),  # prior retry marker count
            ("[60,300,900]",),  # backoff config
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 1

        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        assert failure_inserts[0][1][1] == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        # Subprocess agent did get a retry marker and a crashed flip.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1


# --------------------------------------------------------------------------
# Fix 2 — _resume_retrying_agent filters execution_mode='ecs'
# --------------------------------------------------------------------------


class TestResumeRetryingAgentExecutionModeFilter:
    """The subprocess-lane resume path must skip ECS-mode agents.

    Pre-#3158 the SELECT matched any ``status='retrying'`` row and
    called :meth:`_create_worktree` + :meth:`_run_orchestration_phases`,
    silently forking an ECS agent to subprocess mode.

    Post-#3158 the SELECT filters
    ``COALESCE(execution_mode, 'subprocess') <> 'ecs'``. If no
    subprocess-mode retrying agent exists, the method emits a
    per-observation WARNING for each ECS-retrying row via
    :meth:`_warn_on_ecs_retrying_rows`.
    """

    def test_select_filters_execution_mode_ecs(self, tmp_path: Path) -> None:
        """The resume-scan SELECT excludes ``execution_mode = 'ecs'`` rows."""
        d, conn, _handler = _make_daemon(tmp_path)
        # First SELECT returns no subprocess-mode retrying row.
        # Second SELECT (in _warn_on_ecs_retrying_rows) returns no ECS rows.
        conn.cursor_instance.fetchall_queue = [[]]
        conn.cursor_instance.fetch_queue = [None]

        assert d._resume_retrying_agent() is False

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "status = 'retrying'" in e[0]
            and "dispatcher.agents" in e[0]
            and "execution_mode" in e[0]
        ]
        assert selects, (
            "expected _resume_retrying_agent to issue a SELECT that "
            "filters execution_mode"
        )
        sql, _params = selects[0]
        assert "COALESCE(execution_mode, 'subprocess') <> 'ecs'" in sql, (
            "_resume_retrying_agent must filter execution_mode='ecs'. "
            "Actual SQL: " + sql
        )

    def test_ecs_retrying_row_logs_warning(self, tmp_path: Path) -> None:
        """An ECS-mode retrying row produces an operator-visible WARNING."""
        d, conn, handler = _make_daemon(tmp_path)
        # _resume_retrying_agent issues a SELECT ... WHERE status =
        # 'retrying' AND COALESCE(execution_mode,...) <> 'ecs' and
        # reads via fetchone(). Queue None so the subprocess-lane
        # pickup returns False.
        conn.cursor_instance.fetch_queue = [None]
        # _warn_on_ecs_retrying_rows then issues a second SELECT
        # WHERE execution_mode='ecs' and reads via fetchall().
        conn.cursor_instance.fetchall_queue = [
            [("agent-ecs-retry", 2671)],
        ]

        assert d._resume_retrying_agent() is False

        warn_events = handler.events("agent_ecs_retry_not_supported")
        assert len(warn_events) == 1
        record = warn_events[0]
        assert getattr(record, "agent_id") == "agent-ecs-retry"
        assert getattr(record, "issue_number") == 2671

    def test_subprocess_retrying_agent_still_resumes(self, tmp_path: Path) -> None:
        """A subprocess-mode retrying agent is picked up and resumed."""
        d, conn, _handler = _make_daemon(tmp_path)
        # Stub out the worktree + orchestration so we only exercise
        # the SELECT + UPDATE path.
        d._create_worktree = lambda _agent_id: tmp_path / "wt"  # type: ignore[assignment]
        d._run_orchestration_phases = (  # type: ignore[assignment]
            lambda _agent_id, _issue_number, _worktree: None
        )
        conn.cursor_instance.fetch_queue = [("agent-sub", 2807)]

        assert d._resume_retrying_agent() is True

        # Flip-to-running UPDATE issued — the SQL string contains
        # ``'running'`` (literal, in the SET clause) + the agent_id
        # param tuple.
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and "'running'" in e[0]
            and e[1] == ("agent-sub",)
        ]
        assert updates, (
            f"expected flip-to-running UPDATE, saw: "
            f"{[e[0][:80] for e in conn.cursor_instance.executed]}"
        )


# --------------------------------------------------------------------------
# #3809 — _warn_on_ecs_retrying_rows must emit at most once per agent_id
# --------------------------------------------------------------------------


class TestWarnOnEcsRetryingRowsOnceShotPerAgent:
    """The ``agent_ecs_retry_not_supported`` WARNING must fire once per agent.

    Pre-#3809, :meth:`_warn_on_ecs_retrying_rows` re-emitted the WARN
    on every supervisor tick (~every 10s) for every ECS-mode retrying
    row in the table. With two such rows persistently stuck this
    floods CloudWatch with ~720 WARN lines/hour for the same two
    rows. The legitimate signal is "two ECS agents need cleanup" —
    fire that ONCE per ``(run_id, agent_id)`` pair, not every tick.

    #3809 introduces a per-process ``_observed_ecs_retrying`` set that
    gates emission: an ``agent_id`` already in the set is skipped on
    subsequent ticks. The set is in-memory only — daemon restart
    resets it (and re-emits one WARN per row), which is the right
    cadence for an operator-visible signal.
    """

    def test_warning_emits_once_per_agent_across_five_ticks(
        self, tmp_path: Path
    ) -> None:
        """Stage two ECS-retrying rows, run 5 supervisor ticks, expect 2 warns."""
        d, conn, handler = _make_daemon(tmp_path)
        # Each call to _warn_on_ecs_retrying_rows issues ONE SELECT
        # (`fetchall`) returning the same two rows. Five ticks → five
        # SELECTs returning the same payload.
        rows = [
            ("agent-ecs-1", 3778),
            ("agent-ecs-2", 3641),
        ]
        conn.cursor_instance.fetchall_queue = [rows for _ in range(5)]

        for _ in range(5):
            d._warn_on_ecs_retrying_rows()

        warn_events = handler.events("agent_ecs_retry_not_supported")
        # Exactly TWO warnings — one per distinct agent_id — across
        # all five ticks. Pre-#3809 this asserted-against-10
        # (5 ticks × 2 rows).
        assert len(warn_events) == 2, (
            f"#3809 — expected 2 warnings (one per agent_id) across 5 "
            f"ticks, got {len(warn_events)}. The warning must be gated "
            f"on a per-process set so persistent ECS-retrying rows "
            f"don't flood CloudWatch every tick."
        )
        emitted_agent_ids = sorted(getattr(r, "agent_id") for r in warn_events)
        assert emitted_agent_ids == ["agent-ecs-1", "agent-ecs-2"]

    def test_new_agent_after_first_emission_still_warns(self, tmp_path: Path) -> None:
        """A new ECS-retrying row appearing later still produces a warning."""
        d, conn, handler = _make_daemon(tmp_path)
        # Tick 1: only agent-ecs-1 is retrying.
        # Tick 2: agent-ecs-2 also retrying. Should warn for agent-ecs-2 only.
        # Tick 3: both rows still retrying. No new warnings.
        conn.cursor_instance.fetchall_queue = [
            [("agent-ecs-1", 3778)],
            [("agent-ecs-1", 3778), ("agent-ecs-2", 3641)],
            [("agent-ecs-1", 3778), ("agent-ecs-2", 3641)],
        ]

        for _ in range(3):
            d._warn_on_ecs_retrying_rows()

        warn_events = handler.events("agent_ecs_retry_not_supported")
        emitted_agent_ids = sorted(getattr(r, "agent_id") for r in warn_events)
        assert emitted_agent_ids == ["agent-ecs-1", "agent-ecs-2"], (
            f"#3809 — expected one WARN per distinct agent_id, got {emitted_agent_ids}"
        )


# --------------------------------------------------------------------------
# Fix 3 — _handle_force_stop branches on execution_mode for signal delivery
# --------------------------------------------------------------------------


class TestHandleForceStopExecutionModeBranch:
    """Per-agent force_stop must stop the runtime for BOTH modes.

    Pre-#3158 the method SIGKILLed ``pid`` unconditionally. For ECS
    agents ``pid`` is NULL on the daemon host, so the SIGKILL was a
    no-op while the Fargate task kept running — a zombie state.

    Post-#3158, a ``SELECT pid, execution_mode, agent_task_arn``
    drives the branch: subprocess → SIGKILL, ECS →
    ``ecs:StopTask(agent_task_arn)`` via
    :meth:`_force_stop_ecs_task`.
    """

    def test_subprocess_agent_sigkills_pid(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Subprocess branch SIGKILLs the recorded pid."""
        d, conn, handler = _make_daemon(tmp_path)
        # Record SIGKILL targets.
        killed: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            killed.append((pid, sig))

        monkeypatch.setattr(daemon.os, "kill", fake_kill)
        # Agent row: pid=12345, execution_mode='subprocess', arn=None.
        conn.cursor_instance.fetch_queue = [(12345, "subprocess", None)]

        d._handle_force_stop(conn.cursor_instance, {"agentId": "agent-sub"})

        # UPDATE status='crashed' ran.
        crashed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
        ]
        assert crashed_updates
        # SIGKILL delivered to pid 12345.
        assert killed == [(12345, signal.SIGKILL)]

    def test_ecs_agent_calls_stop_task(self, tmp_path: Path) -> None:
        """ECS branch invokes ``ecs:StopTask`` with the task ARN."""
        task_arn = (
            "arn:aws:ecs:us-west-2:155326049300:task/"
            "judgemind-dev/888fbe0274694bb2baaa8de21cdbe142"
        )
        d, conn, handler = _make_daemon(
            tmp_path,
            ecs_cluster_arn="arn:aws:ecs:us-west-2:155326049300:cluster/judgemind-dev",
        )
        # Stub the ECS client BEFORE the handler runs so
        # _force_stop_ecs_task picks it up instead of calling
        # _make_ecs_client.
        stop_calls: list[dict[str, Any]] = []

        class _FakeEcsClient:
            def stop_task(self, **kwargs: Any) -> dict[str, Any]:
                stop_calls.append(kwargs)
                return {"task": {"taskArn": kwargs["task"]}}

        d._ecs_client = _FakeEcsClient()

        # Agent row: pid=None, execution_mode='ecs', arn populated.
        conn.cursor_instance.fetch_queue = [(None, "ecs", task_arn)]

        d._handle_force_stop(conn.cursor_instance, {"agentId": "agent-ecs"})

        # ``ecs:StopTask`` called once with the cluster + task + reason.
        assert len(stop_calls) == 1
        assert stop_calls[0]["cluster"] == d._cfg.ecs_cluster_arn
        assert stop_calls[0]["task"] == task_arn
        assert stop_calls[0]["reason"] == "operator_force_stop"

        # Structured-log event emitted for CloudWatch.
        sent_events = handler.events("force_stop_ecs_task_sent")
        assert len(sent_events) == 1

    def test_ecs_agent_missing_arn_logs_and_returns(self, tmp_path: Path) -> None:
        """An ECS agent with NULL ``agent_task_arn`` logs + returns cleanly."""
        d, conn, handler = _make_daemon(
            tmp_path,
            ecs_cluster_arn="arn:aws:ecs:us-west-2:155326049300:cluster/judgemind-dev",
        )
        # pid=None, mode='ecs', arn=None (e.g. pre-launch crash).
        conn.cursor_instance.fetch_queue = [(None, "ecs", None)]

        d._handle_force_stop(conn.cursor_instance, {"agentId": "agent-ecs-no-arn"})

        events = handler.events("force_stop_ecs_task_no_arn")
        assert len(events) == 1

    def test_ecs_agent_missing_cluster_config_logs(self, tmp_path: Path) -> None:
        """An ECS agent with an ARN but no cluster config logs a warning."""
        d, conn, handler = _make_daemon(
            tmp_path,
            ecs_cluster_arn=None,  # local dev / tests
        )
        conn.cursor_instance.fetch_queue = [
            (None, "ecs", "arn:aws:ecs:us-west-2:1:task/x/y"),
        ]

        d._handle_force_stop(conn.cursor_instance, {"agentId": "agent-ecs-no-cluster"})

        events = handler.events("force_stop_ecs_task_no_cluster")
        assert len(events) == 1

    def test_missing_agent_raises_command_error(self, tmp_path: Path) -> None:
        """Unchanged behavior for missing agent_id — CommandError."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]

        import pytest

        with pytest.raises(daemon.CommandError):
            d._handle_force_stop(conn.cursor_instance, {"agentId": "agent-missing"})

    def test_select_includes_execution_mode_and_task_arn(self, tmp_path: Path) -> None:
        """The SELECT must fetch mode + arn in a single round-trip."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(12345, "subprocess", None)]

        d._handle_force_stop(conn.cursor_instance, {"agentId": "any"})

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT pid" in e[0] and "dispatcher.agents" in e[0]
        ]
        assert selects, "expected SELECT pid, execution_mode, agent_task_arn"
        sql, _params = selects[0]
        assert "execution_mode" in sql
        assert "agent_task_arn" in sql
