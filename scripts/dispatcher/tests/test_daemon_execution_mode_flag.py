"""Unit tests for issue #3091 — ``dispatcher.config.agent_execution_mode`` flag.

Coverage:

* ``_cb_config_str`` reads a string key with a fallback and handles
  missing rows / malformed JSON / empty values by returning the
  default.
* ``_read_agent_execution_mode`` returns the stored value when it's
  one of the recognized modes (``'subprocess'`` / ``'ecs'``), falls
  back to the default with a warning on an unrecognized value, and
  defaults to ``'ecs'`` when the row is missing.
* Claim-time dispatch branches on the read mode: ``'ecs'`` calls
  ``_launch_agent_ecs_task`` and skips ``_run_orchestration_phases``;
  ``'subprocess'`` takes the legacy path.
* Mode is immutable once written to the agent row — ``_atomic_claim``
  persists it and later flips of the config don't produce hybrid
  agents.
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
    def __init__(self, value: Any = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._value = value

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if self._value is _MISSING:
            return None
        return (self._value,)


_MISSING = object()


class _FakeConn:
    def __init__(self, value: Any = _MISSING) -> None:
        self.cursor_obj = _FakeCursor(value)
        self.committed = 0
        self.rolled_back = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


def _make_daemon(value: Any = _MISSING) -> daemon.DispatcherDaemon:
    import threading

    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    d._main_conn = _FakeConn(value=value)  # type: ignore[attr-defined]
    d._cfg = MagicMock()  # type: ignore[attr-defined]
    d._log = logging.getLogger("test.daemon_execution_mode")  # type: ignore[attr-defined]
    d._run_id = "test-run-id"  # type: ignore[attr-defined]
    return d


# --------------------------------------------------------------------------
# _cb_config_str.
# --------------------------------------------------------------------------


class TestCBConfigStr:
    def test_missing_row_returns_default(self) -> None:
        d = _make_daemon(value=_MISSING)
        assert d._cb_config_str("agent_execution_mode", "subprocess") == "subprocess"

    def test_null_value_returns_default(self) -> None:
        d = _make_daemon(value=None)
        assert d._cb_config_str("agent_execution_mode", "subprocess") == "subprocess"

    def test_bare_string_value(self) -> None:
        d = _make_daemon(value="ecs")
        assert d._cb_config_str("agent_execution_mode", "subprocess") == "ecs"

    def test_json_encoded_string_value(self) -> None:
        """Test fixtures sometimes feed raw JSON (``'"ecs"'``) — we unwrap."""
        d = _make_daemon(value='"ecs"')
        assert d._cb_config_str("agent_execution_mode", "subprocess") == "ecs"

    def test_empty_string_returns_default(self) -> None:
        d = _make_daemon(value="")
        assert d._cb_config_str("agent_execution_mode", "subprocess") == "subprocess"


# --------------------------------------------------------------------------
# _read_agent_execution_mode.
# --------------------------------------------------------------------------


class TestReadAgentExecutionMode:
    def test_default_is_ecs(self) -> None:
        d = _make_daemon(value=_MISSING)
        assert d._read_agent_execution_mode() == "ecs"

    def test_recognizes_ecs(self) -> None:
        d = _make_daemon(value="ecs")
        assert d._read_agent_execution_mode() == "ecs"

    def test_unrecognized_falls_back_with_warning(self, caplog: Any) -> None:
        d = _make_daemon(value="docker")
        caplog.set_level(logging.WARNING, logger="test.daemon_execution_mode")
        assert d._read_agent_execution_mode() == "ecs"
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "agent_execution_mode_unrecognized" in events


# --------------------------------------------------------------------------
# Claim-time branching — the immutability + dispatch contract.
# --------------------------------------------------------------------------


class TestClaimTimeDispatch:
    def test_ecs_mode_launches_ecs_task_and_skips_orchestration(self) -> None:
        """When mode='ecs', _claim_and_orchestrate_one MUST call
        _launch_agent_ecs_task and MUST NOT call
        _run_orchestration_phases or _create_worktree.
        """
        d = _make_daemon()
        # Stub all the helpers _claim_and_orchestrate_one touches.
        with (
            patch.object(d, "_resume_retrying_agent", return_value=False),
            patch.object(
                d,
                "_latest_queue_snapshot_issues",
                return_value=[{"number": 500}],
            ),
            patch.object(d, "_pick_candidate_issue", return_value=500),
            patch.object(d, "_latest_queue_snapshot_title_for", return_value=None),
            patch.object(d, "_latest_queue_snapshot_priority_for", return_value="p2"),
            patch.object(d, "_read_agent_execution_mode", return_value="ecs"),
            patch.object(d, "_atomic_claim", return_value=True) as claim_mock,
            patch.object(
                d, "_launch_agent_ecs_task", return_value="arn:ecs-task"
            ) as launch_mock,
            patch.object(d, "_create_worktree") as worktree_mock,
            patch.object(d, "_run_orchestration_phases") as orch_mock,
            patch.object(d, "_handle_agent_failure") as fail_mock,
            patch.object(d, "_compute_worktree_path", return_value=Path("/tmp/wt")),
        ):
            d._claim_and_orchestrate_one()
        # Claim call threads execution_mode='ecs' through.
        claim_mock.assert_called_once()
        kwargs = claim_mock.call_args.kwargs
        assert kwargs.get("execution_mode") == "ecs"
        # ECS launcher fired.
        launch_mock.assert_called_once()
        # Legacy path was NOT taken.
        worktree_mock.assert_not_called()
        orch_mock.assert_not_called()
        fail_mock.assert_not_called()

    def test_ecs_mode_launch_failure_routes_to_handle_agent_failure(self) -> None:
        d = _make_daemon()
        with (
            patch.object(d, "_resume_retrying_agent", return_value=False),
            patch.object(
                d,
                "_latest_queue_snapshot_issues",
                return_value=[{"number": 700}],
            ),
            patch.object(d, "_pick_candidate_issue", return_value=700),
            patch.object(d, "_latest_queue_snapshot_title_for", return_value=None),
            patch.object(d, "_latest_queue_snapshot_priority_for", return_value="p2"),
            patch.object(d, "_read_agent_execution_mode", return_value="ecs"),
            patch.object(d, "_atomic_claim", return_value=True),
            # Launch returned None => exhausted or wiring missing.
            patch.object(d, "_launch_agent_ecs_task", return_value=None),
            patch.object(d, "_create_worktree") as worktree_mock,
            patch.object(d, "_run_orchestration_phases") as orch_mock,
            patch.object(d, "_handle_agent_failure") as fail_mock,
            patch.object(d, "_compute_worktree_path", return_value=Path("/tmp/wt")),
        ):
            d._claim_and_orchestrate_one()
        worktree_mock.assert_not_called()
        orch_mock.assert_not_called()
        fail_mock.assert_called_once()
        kw = fail_mock.call_args.kwargs
        assert kw["category"] == "agent_task_launch_failed"
        assert kw["phase"] == "claiming"
        assert kw["issue_number"] == 700

    def test_subprocess_mode_takes_legacy_path(self) -> None:
        """When mode='subprocess' (default), the legacy worktree +
        orchestration path runs; _launch_agent_ecs_task must NOT fire.
        """
        d = _make_daemon()
        with (
            patch.object(d, "_resume_retrying_agent", return_value=False),
            patch.object(
                d,
                "_latest_queue_snapshot_issues",
                return_value=[{"number": 900}],
            ),
            patch.object(d, "_pick_candidate_issue", return_value=900),
            patch.object(d, "_latest_queue_snapshot_title_for", return_value=None),
            patch.object(d, "_latest_queue_snapshot_priority_for", return_value="p2"),
            patch.object(d, "_read_agent_execution_mode", return_value="subprocess"),
            patch.object(d, "_atomic_claim", return_value=True) as claim_mock,
            patch.object(d, "_launch_agent_ecs_task") as launch_mock,
            patch.object(d, "_create_worktree", return_value=Path("/tmp/wt")),
            patch.object(d, "_run_orchestration_phases") as orch_mock,
            patch.object(d, "_compute_worktree_path", return_value=Path("/tmp/wt")),
        ):
            d._claim_and_orchestrate_one()
        launch_mock.assert_not_called()
        orch_mock.assert_called_once()
        # Claim carries the subprocess mode through.
        assert claim_mock.call_args.kwargs.get("execution_mode") == "subprocess"


# --------------------------------------------------------------------------
# Immutability — _atomic_claim writes the chosen mode at claim time.
# --------------------------------------------------------------------------


class TestExecutionModePersistenceAtomicClaim:
    def test_atomic_claim_persists_execution_mode(self) -> None:
        """The INSERT into dispatcher.agents MUST include execution_mode
        as a parameter so the column is set authoritatively at claim
        time (not relying on the DEFAULT from migration 41).
        """
        d = _make_daemon()
        # _atomic_claim calls _gh_issue_add_labels post-INSERT; stub to
        # avoid GitHub calls in tests.
        with patch.object(d, "_gh_issue_add_labels"):
            ok = d._atomic_claim(
                issue_number=42,
                agent_id="agent-x",
                worktree_path="/tmp/wt",
                execution_mode="ecs",
            )
        assert ok is True
        # Check the INSERT SQL shape.
        executed = d._main_conn.cursor_obj.executed  # type: ignore[attr-defined]
        assert len(executed) == 1
        sql, params = executed[0]
        assert "INSERT INTO dispatcher.agents" in sql
        assert "execution_mode" in sql
        # The last param should be 'ecs'.
        assert params[-1] == "ecs"

    def test_atomic_claim_default_mode_is_ecs(self) -> None:
        """Callers that don't pass execution_mode get the module-level
        default — ``'ecs'`` since #3093 (Stage 4 flip).
        """
        d = _make_daemon()
        with patch.object(d, "_gh_issue_add_labels"):
            d._atomic_claim(
                issue_number=1,
                agent_id="agent-y",
                worktree_path="/tmp/wt",
            )
        _sql, params = d._main_conn.cursor_obj.executed[0]  # type: ignore[attr-defined]
        assert params[-1] == "ecs"
