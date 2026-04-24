"""Unit tests for the ECS-agent verify short-circuit (Issue #3196).

The daemon's supervisor tick must NOT call
:meth:`DispatcherDaemon._run_verify_and_complete` for ECS-mode agents.
The per-agent Fargate ``agent-runner-entrypoint.sh`` runs verify in
its own state machine (#3176 Stage 3); the daemon's supervisor
previously spawned ``claude -p /task-v2-verify`` as a subprocess AND
``subprocess.wait()``-ed for the full verify duration ON THE
MainThread. That blocked ``scheduler_tick`` for multiple minutes,
made ``dispatcher.config.concurrency_cap > 1`` inoperative (no new
claims could be processed), and tripped the 5-minute
``scheduler_tick_stalled`` watchdog.

The fix adds two short-circuits:

1. :meth:`_advance_running_agents` skips ECS agents entirely (with an
   ``supervise_skipped_ecs_agent`` log event, or
   ``verify_skipped_ecs_agent`` specifically for the
   ``awaiting_deploy`` phase that carried the original regression).
2. :meth:`_advance_awaiting_deploy` has a belt-and-suspenders ECS
   guard for direct-call paths (tests, recovery, future refactors).

Both layers are tested here. External calls (``gh``, ``git``,
``claude -p``, psycopg) are mocked — no real LLM invoked.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Fixtures — minimal fakes matching the shape used in test_daemon_phase3b.
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

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass


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
    logger = logging.getLogger(f"dispatcher.test.verify_skip_ecs.{id(tmp_path)}")
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
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# _advance_awaiting_deploy — belt-and-suspenders ECS guard
# --------------------------------------------------------------------------


class TestAdvanceAwaitingDeployEcsGuard:
    """Direct-call ECS guard inside ``_advance_awaiting_deploy`` itself."""

    def test_ecs_agent_short_circuits_before_pr_fetch(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """An ECS agent that reaches this method directly must NOT fetch
        PR status, poll deploy runs, or call _run_verify_and_complete.
        """
        d, _conn, handler = _make_daemon(tmp_path)

        fetch_pr_calls: list[int] = []
        monkeypatch.setattr(
            d, "_fetch_pr_status", lambda n: fetch_pr_calls.append(n) or {}
        )
        verify_calls: list[Any] = []
        monkeypatch.setattr(
            d,
            "_run_verify_and_complete",
            lambda agent, pr_status, merge_sha, deploy_runs: verify_calls.append(agent),
        )

        agent = {
            "agent_id": "ecs-agent-1",
            "issue_number": 3193,
            "phase": "awaiting_deploy",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
            "status": "succeeded",
            "execution_mode": "ecs",
        }
        d._advance_awaiting_deploy(agent)

        # The ECS short-circuit must fire before any PR / verify work.
        assert fetch_pr_calls == [], (
            "ECS agent must not trigger _fetch_pr_status — "
            f"got calls for PR numbers {fetch_pr_calls}"
        )
        assert verify_calls == [], (
            "ECS agent must NOT trigger _run_verify_and_complete — "
            "that path blocks scheduler_tick on MainThread (Issue #3196)"
        )
        # The short-circuit event is the grep hook for
        # CloudWatch-side verification.
        skipped = handler.events("verify_skipped_ecs_agent")
        assert len(skipped) == 1
        assert skipped[0].agent_id == "ecs-agent-1"
        assert skipped[0].pr_number == 101

    def test_subprocess_agent_still_runs_verify_path(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Subprocess agents must still reach _run_verify_and_complete —
        the guard is for ECS only.
        """
        d, _conn, handler = _make_daemon(tmp_path)

        monkeypatch.setattr(
            d,
            "_fetch_pr_status",
            lambda n: {
                "statusCheckRollup": [],
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "headRefOid": "head-sha",
                "mergeCommit": {"oid": "merge-sha"},
            },
        )
        monkeypatch.setattr(d, "_find_deploy_runs", lambda sha: [])
        verify_calls: list[Any] = []
        monkeypatch.setattr(
            d,
            "_run_verify_and_complete",
            lambda agent, pr_status, merge_sha, deploy_runs: verify_calls.append(agent),
        )

        agent = {
            "agent_id": "sub-agent-1",
            "issue_number": 3199,
            "phase": "awaiting_deploy",
            "pr_number": 102,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
            "status": "succeeded",
            "execution_mode": "subprocess",
        }
        d._advance_awaiting_deploy(agent)

        assert len(verify_calls) == 1, (
            "Subprocess agent must still reach _run_verify_and_complete — "
            "the #3196 ECS guard must not accidentally short-circuit "
            "subprocess-mode agents"
        )
        assert handler.events("verify_skipped_ecs_agent") == []

    def test_missing_execution_mode_defaults_to_subprocess(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Agents without execution_mode key default to subprocess lane.

        Handles the recovery path where a test or legacy row lacks the
        column. Defaulting to subprocess preserves legacy behaviour —
        the ECS short-circuit is strictly opt-in via an explicit
        ``execution_mode='ecs'`` marker.
        """
        d, _conn, handler = _make_daemon(tmp_path)

        monkeypatch.setattr(
            d,
            "_fetch_pr_status",
            lambda n: {
                "statusCheckRollup": [],
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "headRefOid": "head-sha",
                "mergeCommit": {"oid": "merge-sha"},
            },
        )
        monkeypatch.setattr(d, "_find_deploy_runs", lambda sha: [])
        verify_calls: list[Any] = []
        monkeypatch.setattr(
            d,
            "_run_verify_and_complete",
            lambda agent, pr_status, merge_sha, deploy_runs: verify_calls.append(agent),
        )

        agent = {
            "agent_id": "legacy-agent",
            "issue_number": 42,
            "phase": "awaiting_deploy",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": 0,
            "status": "succeeded",
            # No execution_mode key at all.
        }
        d._advance_awaiting_deploy(agent)

        assert len(verify_calls) == 1
        assert handler.events("verify_skipped_ecs_agent") == []


# --------------------------------------------------------------------------
# _advance_running_agents — top-level ECS skip
# --------------------------------------------------------------------------


class TestAdvanceRunningAgentsEcsSkip:
    """The supervisor loop itself must filter ECS agents before dispatch."""

    def test_ecs_agent_awaiting_deploy_logs_verify_skipped(
        self, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        advance_calls: dict[str, Any] = {}
        d._advance_awaiting_ci = lambda agent: advance_calls.setdefault(  # type: ignore[method-assign]
            "ci", agent
        )
        d._advance_awaiting_deploy = lambda agent: advance_calls.setdefault(  # type: ignore[method-assign]
            "deploy", agent
        )
        d._run_retro_phase = lambda agent: advance_calls.setdefault(  # type: ignore[method-assign]
            "retro", agent
        )
        d._cleanup_agent_worktree = lambda agent: advance_calls.setdefault(  # type: ignore[method-assign]
            "cleanup", agent
        )
        d._list_advanceable_agents = lambda: [  # type: ignore[method-assign]
            {
                "agent_id": "ecs-agent-1",
                "issue_number": 3193,
                "phase": "awaiting_deploy",
                "pr_number": 101,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
                "status": "succeeded",
                "execution_mode": "ecs",
            }
        ]

        advanced = d._advance_running_agents()

        # The ECS agent counts as neither advanced nor in-error — it's
        # silently skipped (with a log event).
        assert advanced == 0
        # No phase handler was dispatched.
        assert advance_calls == {}
        # Specifically the verify-name event fired (AC #2 grep hook).
        skipped = handler.events("verify_skipped_ecs_agent")
        assert len(skipped) == 1
        assert skipped[0].agent_id == "ecs-agent-1"

    def test_ecs_agent_other_phase_logs_generic_skip(self, tmp_path: Path) -> None:
        """Non-awaiting_deploy ECS skips fire the generic event name."""
        d, _conn, handler = _make_daemon(tmp_path)

        retro_calls: list[Any] = []
        d._run_retro_phase = lambda agent: retro_calls.append(agent)  # type: ignore[method-assign]
        d._list_advanceable_agents = lambda: [  # type: ignore[method-assign]
            {
                "agent_id": "ecs-agent-2",
                "issue_number": 3193,
                "phase": "done",
                "pr_number": 101,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
                "status": "succeeded",
                "execution_mode": "ecs",
            }
        ]

        d._advance_running_agents()

        # Generic skip event for non-awaiting_deploy phases.
        generic = handler.events("supervise_skipped_ecs_agent")
        assert len(generic) == 1
        assert generic[0].phase == "done"
        assert retro_calls == [], (
            "ECS agent reached _run_retro_phase despite the skip — "
            "that path spawns /task-v2-retro on the MainThread, which "
            "this fix exists to prevent"
        )

    def test_subprocess_agent_awaiting_deploy_not_skipped(self, tmp_path: Path) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        deploy_calls: list[Any] = []
        d._advance_awaiting_deploy = lambda agent: deploy_calls.append(agent)  # type: ignore[method-assign]
        d._list_advanceable_agents = lambda: [  # type: ignore[method-assign]
            {
                "agent_id": "sub-agent-1",
                "issue_number": 3199,
                "phase": "awaiting_deploy",
                "pr_number": 102,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
                "status": "succeeded",
                "execution_mode": "subprocess",
            }
        ]

        advanced = d._advance_running_agents()

        assert advanced == 1
        assert len(deploy_calls) == 1
        assert handler.events("verify_skipped_ecs_agent") == []
        assert handler.events("supervise_skipped_ecs_agent") == []

    def test_mixed_ecs_and_subprocess_agents(self, tmp_path: Path) -> None:
        """Subprocess agents still advance even when ECS agents are present."""
        d, _conn, handler = _make_daemon(tmp_path)

        ci_calls: list[Any] = []
        deploy_calls: list[Any] = []
        d._advance_awaiting_ci = lambda agent: ci_calls.append(agent)  # type: ignore[method-assign]
        d._advance_awaiting_deploy = lambda agent: deploy_calls.append(agent)  # type: ignore[method-assign]
        d._list_advanceable_agents = lambda: [  # type: ignore[method-assign]
            {
                "agent_id": "ecs-1",
                "issue_number": 1,
                "phase": "awaiting_deploy",
                "pr_number": 101,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
                "status": "succeeded",
                "execution_mode": "ecs",
            },
            {
                "agent_id": "sub-1",
                "issue_number": 2,
                "phase": "awaiting_ci",
                "pr_number": 102,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
                "status": "running",
                "execution_mode": "subprocess",
            },
            {
                "agent_id": "ecs-2",
                "issue_number": 3,
                "phase": "awaiting_ci",
                "pr_number": 103,
                "worktree_path": str(tmp_path),
                "retries_used": 0,
                "status": "running",
                "execution_mode": "ecs",
            },
        ]

        advanced = d._advance_running_agents()

        # Only the subprocess agent advanced.
        assert advanced == 1
        assert len(ci_calls) == 1
        assert ci_calls[0]["agent_id"] == "sub-1"
        assert deploy_calls == []
        # Two skip events fired — one with verify name (awaiting_deploy),
        # one generic (awaiting_ci).
        assert len(handler.events("verify_skipped_ecs_agent")) == 1
        assert len(handler.events("supervise_skipped_ecs_agent")) == 1


# --------------------------------------------------------------------------
# _list_advanceable_agents — execution_mode threaded through SELECT
# --------------------------------------------------------------------------


class TestListAdvanceableAgentsExecutionMode:
    def test_select_reads_execution_mode_column(self, tmp_path: Path) -> None:
        """The SELECT must surface execution_mode so the dispatch can branch."""
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetchall_queue = [[]]
        d._list_advanceable_agents()

        selects = [
            e
            for e in conn.cursor_instance.executed
            if "SELECT agent_id" in e[0] and "dispatcher.agents" in e[0]
        ]
        assert selects, "expected SELECT against dispatcher.agents"
        sql, _params = selects[0]
        # Issue #3196: ``execution_mode`` threaded through SELECT so
        # the dispatcher can skip ECS agents in the supervisor loop.
        assert "execution_mode" in sql, (
            "SELECT must surface execution_mode so _advance_running_agents "
            "can filter ECS agents. Actual SQL: " + sql
        )

    def test_rows_populate_execution_mode_key(self, tmp_path: Path) -> None:
        """Rows returned from _list_advanceable_agents must include
        ``execution_mode`` in the dict (so per-phase handlers can branch
        defensively on the same field).
        """
        d, conn, _handler = _make_daemon(tmp_path)
        # 9-column row (agent_id, issue_number, phase, pr_number,
        # worktree_path, retries_used, status, merge_unstick_attempts,
        # execution_mode).
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "agent-ecs",
                    42,
                    "awaiting_deploy",
                    101,
                    "/tmp/w",
                    0,
                    "succeeded",
                    0,
                    "ecs",
                ),
                (
                    "agent-sub",
                    43,
                    "awaiting_ci",
                    102,
                    "/tmp/w",
                    0,
                    "running",
                    0,
                    "subprocess",
                ),
            ]
        ]
        rows = d._list_advanceable_agents()
        assert len(rows) == 2
        assert rows[0]["execution_mode"] == "ecs"
        assert rows[1]["execution_mode"] == "subprocess"

    def test_missing_execution_mode_column_defaults_to_subprocess(
        self, tmp_path: Path
    ) -> None:
        """Pre-#3091 rows (no execution_mode column) default to subprocess.

        If a fake cursor / old fixture returns fewer columns, the dict
        must still carry a safe default so the dispatch logic does not
        KeyError.
        """
        d, conn, _handler = _make_daemon(tmp_path)
        # 8-column row (pre-#3091 shape).
        conn.cursor_instance.fetchall_queue = [
            [
                (
                    "legacy-agent",
                    42,
                    "awaiting_deploy",
                    101,
                    "/tmp/w",
                    0,
                    "succeeded",
                    0,
                )
            ]
        ]
        rows = d._list_advanceable_agents()
        assert len(rows) == 1
        assert rows[0]["execution_mode"] == "subprocess"
