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
# Fix 1 — _check_stuck_agents filters execution_mode='ecs'
# --------------------------------------------------------------------------


class TestCheckStuckAgentsExecutionModeFilter:
    """The supervisor stuck-timeout scan must not flag ECS-mode agents.

    Pre-#3158, the SELECT returned every ``status='running'`` row and
    applied the per-phase threshold uniformly. For a long ECS ralph
    exceeding 15h the scan would flip the ECS row to ``crashed`` and
    enqueue a retry marker — which (absent the #3158 fix in
    :meth:`_resume_retrying_agent`) would silently fork the agent to
    subprocess mode on the next scheduler tick.

    Post-#3158, the SELECT carries
    ``COALESCE(a.execution_mode, 'subprocess') <> 'ecs'`` so ECS rows
    never reach the per-phase timer. An ECS task that's actually hung
    is caught by :meth:`_reap_completed_agent_tasks` when ECS itself
    stops it.
    """

    def test_select_filters_execution_mode_ecs(self, tmp_path: Path) -> None:
        """The SELECT excludes ``execution_mode = 'ecs'`` via COALESCE."""
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
        assert "COALESCE(a.execution_mode, 'subprocess') <> 'ecs'" in sql, (
            "_check_stuck_agents must filter out execution_mode='ecs' rows. "
            "Actual SQL: " + sql
        )

    def test_subprocess_agent_still_flagged_on_timeout(self, tmp_path: Path) -> None:
        """A subprocess-mode agent stuck past its threshold still flips crashed."""
        d, conn, handler = _make_daemon(tmp_path)
        # Override the config read so the per-phase overrides lookup
        # returns an empty dict and we fall back to module defaults
        # (``ralph`` = 54000s).
        d._read_stuck_timeout_overrides = lambda: {}  # type: ignore[assignment]
        # One subprocess agent stuck 60000s in ralph (exceeds 54000s).
        conn.cursor_instance.fetchall_queue = [
            [("agent-sub", 2807, "ralph", 60000.0)],
        ]
        # Retry marker COUNT + backoff config reads that
        # _write_failure / _create_retry_marker issue.
        conn.cursor_instance.fetch_queue = [
            (0,),
            ("[60,300,900]",),
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
