"""Issue #3366 — assertions for the ``next_directive`` consumer.

Three states the consumer must distinguish:

* ``respawn_at=<phase>`` — daemon launches a fresh agent-runner ECS
  task with ``START_PHASE=<phase>`` env. Phase is validated against
  :data:`AGENT_RUNNER_VALID_START_PHASES`.
* ``terminal`` — daemon frees the slot, logs
  ``daemon.diagnoser_completed_terminal``.
* NULL / malformed / unknown phase — daemon falls back to escalate
  AND logs ``daemon.diagnoser_did_not_complete``. Distinguishable from
  ``terminal`` is the key invariant.
"""

# Issue numbers in fixtures (e.g. ``99001``) are synthetic.

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Make ``scripts`` importable.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402
from dispatcher.daemon import (  # noqa: E402
    AGENT_RUNNER_VALID_START_PHASES,
    DIRECTIVE_RESPAWN_AT_PREFIX,
    DIRECTIVE_TERMINAL,
)


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
        self.rowcount = 1

    def fetchone(self) -> Any:
        if self.fetch_queue:
            return self.fetch_queue.pop(0)
        return None

    def fetchall(self) -> list[Any]:
        if self.fetchall_queue:
            return self.fetchall_queue.pop(0)
        return []


class _FakeConn:
    def __init__(self, cursor: _FakeCursor | None = None) -> None:
        self._cursor = cursor or _FakeCursor()
        self.committed = 0
        self.rolled_back = 0

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


def _make_daemon(cursor: _FakeCursor | None = None) -> daemon.DispatcherDaemon:
    import threading

    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    d._main_conn = _FakeConn(cursor)  # type: ignore[attr-defined]
    d._cfg = MagicMock(github_repo="judgemind/judgemind")  # type: ignore[attr-defined]
    d._log = logging.getLogger("test.daemon")  # type: ignore[attr-defined]
    d._run_id = "test-run"  # type: ignore[attr-defined]
    return d


# --------------------------------------------------------------------------
# _parse_next_directive
# --------------------------------------------------------------------------


class TestParseNextDirective:
    def test_terminal(self) -> None:
        d = _make_daemon()
        kind, payload = d._parse_next_directive(DIRECTIVE_TERMINAL)  # type: ignore[attr-defined]
        assert kind == "terminal"
        assert payload is None

    def test_terminal_with_whitespace(self) -> None:
        d = _make_daemon()
        kind, payload = d._parse_next_directive("  terminal  ")  # type: ignore[attr-defined]
        assert kind == "terminal"
        assert payload is None

    def test_respawn_at_valid_phase(self) -> None:
        d = _make_daemon()
        for phase in AGENT_RUNNER_VALID_START_PHASES:
            kind, payload = d._parse_next_directive(  # type: ignore[attr-defined]
                f"{DIRECTIVE_RESPAWN_AT_PREFIX}{phase}"
            )
            assert kind == "respawn_at"
            assert payload == phase

    def test_respawn_at_unknown_phase(self) -> None:
        d = _make_daemon()
        kind, payload = d._parse_next_directive("respawn_at=bogus")  # type: ignore[attr-defined]
        assert kind is None
        assert payload is None

    def test_respawn_at_empty_phase(self) -> None:
        d = _make_daemon()
        kind, payload = d._parse_next_directive("respawn_at=")  # type: ignore[attr-defined]
        assert kind is None
        assert payload is None

    def test_unknown_directive_string(self) -> None:
        d = _make_daemon()
        kind, payload = d._parse_next_directive("retry_again")  # type: ignore[attr-defined]
        assert kind is None
        assert payload is None

    def test_none(self) -> None:
        d = _make_daemon()
        kind, payload = d._parse_next_directive(None)  # type: ignore[attr-defined]
        assert kind is None
        assert payload is None

    def test_empty_string(self) -> None:
        d = _make_daemon()
        kind, payload = d._parse_next_directive("")  # type: ignore[attr-defined]
        assert kind is None
        assert payload is None


# --------------------------------------------------------------------------
# _read_next_directive
# --------------------------------------------------------------------------


class TestReadNextDirective:
    def test_returns_string_when_present(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [("respawn_at=ralph",)]
        d = _make_daemon(cur)
        result = d._read_next_directive(42)  # type: ignore[attr-defined]
        assert result == "respawn_at=ralph"

    def test_returns_none_when_null(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [(None,)]
        d = _make_daemon(cur)
        result = d._read_next_directive(42)  # type: ignore[attr-defined]
        assert result is None

    def test_returns_none_when_row_missing(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = []
        d = _make_daemon(cur)
        result = d._read_next_directive(42)  # type: ignore[attr-defined]
        assert result is None


# --------------------------------------------------------------------------
# _consume_next_directive — the integration point
# --------------------------------------------------------------------------


class TestConsumeNextDirective:
    def _candidate(
        self, agent_id: str = "agent-a", issue_number: int = 99001
    ) -> dict[str, Any]:
        return {
            "agent_id": agent_id,
            "issue_number": issue_number,
            "category": "agent_runner_route_stub",
            "tier": 3,
            "failure_id": 1,
        }

    def test_respawn_at_launches_ecs_task_with_start_phase(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [
            ("respawn_at=ralph",),  # _read_next_directive
        ]
        d = _make_daemon(cur)
        with patch.object(
            d, "_launch_agent_ecs_task", return_value="arn:aws:ecs:..."
        ) as mock_launch:
            outcome = d._consume_next_directive(  # type: ignore[attr-defined]
                diagnosis_id=42,
                candidate=self._candidate(),
                consumed_action="retry",
            )

        assert outcome == "respawn_at_ralph"
        mock_launch.assert_called_once_with(
            "agent-a",
            99001,
            start_phase="ralph",
        )

    def test_terminal_logs_and_strips_label(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [("terminal",)]
        d = _make_daemon(cur)
        d._gh_issue_remove_labels = MagicMock()  # type: ignore[attr-defined]

        outcome = d._consume_next_directive(  # type: ignore[attr-defined]
            diagnosis_id=42,
            candidate=self._candidate(),
            consumed_action="escalate",
        )

        assert outcome == "terminal"
        d._gh_issue_remove_labels.assert_called_once_with(  # type: ignore[attr-defined]
            99001, ["status/in-progress"]
        )

    def test_absent_directive_returns_absent_no_double_escalate(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [(None,)]  # NULL directive
        d = _make_daemon(cur)
        with patch.object(d, "_consume_action_escalate") as mock_escalate:
            outcome = d._consume_next_directive(  # type: ignore[attr-defined]
                diagnosis_id=42,
                candidate=self._candidate(),
                consumed_action="retry",
            )

        assert outcome == "absent"
        # Critical: the directive consumer does NOT run a second escalate
        # — the recommendation-driven action already executed in
        # _consume_diagnosis. The "absent" branch is a pure log signal.
        mock_escalate.assert_not_called()

    def test_malformed_directive_returns_absent(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [("garbage_value",)]
        d = _make_daemon(cur)

        outcome = d._consume_next_directive(  # type: ignore[attr-defined]
            diagnosis_id=42,
            candidate=self._candidate(),
            consumed_action="retry",
        )
        assert outcome == "absent"

    def test_respawn_at_unknown_phase_falls_back_to_absent(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [("respawn_at=invalid_phase",)]
        d = _make_daemon(cur)

        outcome = d._consume_next_directive(  # type: ignore[attr-defined]
            diagnosis_id=42,
            candidate=self._candidate(),
            consumed_action="retry",
        )
        assert outcome == "absent"

    def test_respawn_at_launch_failure_falls_back_to_escalate(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [
            ("respawn_at=ralph",),  # _read_next_directive
        ]
        d = _make_daemon(cur)
        # Launch returns None — capacity exhausted, IAM error, etc.
        with patch.object(d, "_launch_agent_ecs_task", return_value=None):
            with patch.object(d, "_consume_action_escalate") as mock_escalate:
                outcome = d._consume_next_directive(  # type: ignore[attr-defined]
                    diagnosis_id=42,
                    candidate=self._candidate(),
                    consumed_action="retry",
                )

        assert outcome == "respawn_at_launch_failed"
        # Falls back to escalate so the agent does NOT stay zombie-running.
        mock_escalate.assert_called_once()
        kwargs = mock_escalate.call_args.kwargs
        assert kwargs["agent_id"] == "agent-a"
        assert kwargs["issue_number"] == 99001
        assert "respawn_at=ralph" in kwargs["reasoning"]

    def test_respawn_at_resets_agent_row_before_launch(self) -> None:
        """The reset UPDATE clears ended_at + sets phase + status='running'."""
        cur = _FakeCursor()
        cur.fetch_queue = [
            ("respawn_at=ralph",),  # _read_next_directive
        ]
        d = _make_daemon(cur)
        with patch.object(d, "_launch_agent_ecs_task", return_value="arn:..."):
            d._consume_next_directive(  # type: ignore[attr-defined]
                diagnosis_id=42,
                candidate=self._candidate(),
                consumed_action="retry",
            )

        # An UPDATE on dispatcher.agents that sets phase = ralph fired.
        update_sqls = [
            sql for sql, _ in cur.executed if "UPDATE dispatcher.agents" in sql
        ]
        assert update_sqls, "expected an UPDATE on dispatcher.agents"
        assert any("phase = %s" in sql for sql in update_sqls)
        assert any("ended_at = NULL" in sql for sql in update_sqls)
        assert any("status = 'running'" in sql for sql in update_sqls)


# --------------------------------------------------------------------------
# _launch_agent_ecs_task with start_phase env
# --------------------------------------------------------------------------


class TestLaunchAgentEcsTaskStartPhase:
    """The respawn directive sets START_PHASE on the container env."""

    def test_start_phase_added_to_environment(self) -> None:
        d = _make_daemon()
        d._cfg = MagicMock(  # type: ignore[attr-defined]
            ecs_cluster_arn="arn:cluster",
            agent_runner_task_definition_family="agent-runner-dev",
            agent_runner_subnet_ids=["subnet-1"],
            agent_runner_security_group_id="sg-1",
        )
        d._ecs_client = MagicMock()  # type: ignore[attr-defined]
        d._ecs_client.run_task.return_value = {
            "tasks": [{"taskArn": "arn:task"}],
            "failures": [],
        }
        d._agent_runner_wiring_ready = lambda: True  # type: ignore[attr-defined]
        d._persist_agent_task_arn = MagicMock()  # type: ignore[attr-defined]

        d._launch_agent_ecs_task("agent-a", 99001, start_phase="ralph")  # type: ignore[attr-defined]

        run_task_call = d._ecs_client.run_task.call_args
        env_pairs = run_task_call.kwargs["overrides"]["containerOverrides"][0][
            "environment"
        ]
        names = {p["name"]: p["value"] for p in env_pairs}
        assert names["AGENT_ID"] == "agent-a"
        assert names["ISSUE_NUMBER"] == "99001"
        assert names["START_PHASE"] == "ralph"

    def test_start_phase_omitted_by_default(self) -> None:
        d = _make_daemon()
        d._cfg = MagicMock(  # type: ignore[attr-defined]
            ecs_cluster_arn="arn:cluster",
            agent_runner_task_definition_family="agent-runner-dev",
            agent_runner_subnet_ids=["subnet-1"],
            agent_runner_security_group_id="sg-1",
        )
        d._ecs_client = MagicMock()  # type: ignore[attr-defined]
        d._ecs_client.run_task.return_value = {
            "tasks": [{"taskArn": "arn:task"}],
            "failures": [],
        }
        d._agent_runner_wiring_ready = lambda: True  # type: ignore[attr-defined]
        d._persist_agent_task_arn = MagicMock()  # type: ignore[attr-defined]

        d._launch_agent_ecs_task("agent-a", 99001)  # type: ignore[attr-defined]

        run_task_call = d._ecs_client.run_task.call_args
        env_pairs = run_task_call.kwargs["overrides"]["containerOverrides"][0][
            "environment"
        ]
        names = {p["name"]: p["value"] for p in env_pairs}
        assert "START_PHASE" not in names
        assert names["AGENT_ID"] == "agent-a"
