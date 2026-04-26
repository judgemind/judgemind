"""Issue #3366 — assertions for the bypassed-terminal routing in
``_reap_completed_agent_tasks``.

Pre-#3366, when the agent-runner entrypoint wrote a terminal phase of
``conflict_unresolvable`` or ``agent_runner_route_stub`` with
``status='failed'`` and exited 0, the daemon's reaper observed
``task_success=true`` AND ``current_status='failed'`` (in the terminal
set), and ran the bookkeeping-only ``_reap_finalize_ecs_success`` —
NO ``dispatcher.failures`` row was written, so
``_find_diagnoser_candidates`` never picked the terminal up.

This module asserts the post-#3366 routing: those two phases now go
through ``_handle_agent_failure`` with the right category, which writes
the failures row and lets the diagnoser sweep run.

Also asserts:

* ``BYPASSED_TERMINAL_PHASES_TO_ROUTE`` maps the right phases to the
  right category constants.
* ``FAILURE_CATEGORY_AGENT_RUNNER_ROUTE_STUB`` is in
  :data:`TIER_3_CATEGORIES` so the candidate finder treats it as
  diagnose-immediately.
* ``_build_bypassed_terminal_details`` reads the right phase_outputs
  rows for each terminal type.
"""

# Issue numbers in fixtures (e.g. ``99001``) are synthetic. See the
# de-reification rationale in test_daemon_diagnoser_routing.py.

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
    BYPASSED_TERMINAL_PHASES_TO_ROUTE,
    FAILURE_CATEGORY_AGENT_RUNNER_ROUTE_STUB,
    FAILURE_CATEGORY_CONFLICT_UNRESOLVABLE,
    TIER_3_CATEGORIES,
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
    """Construct a DispatcherDaemon with a fake conn for unit tests."""
    import threading

    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    d._main_conn = _FakeConn(cursor)  # type: ignore[attr-defined]
    d._cfg = MagicMock(github_repo="judgemind/judgemind")  # type: ignore[attr-defined]
    d._log = logging.getLogger("test.daemon")  # type: ignore[attr-defined]
    d._run_id = "test-run"  # type: ignore[attr-defined]
    return d


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------


class TestBypassedTerminalConstants:
    def test_route_stub_in_tier_3(self) -> None:
        assert FAILURE_CATEGORY_AGENT_RUNNER_ROUTE_STUB in TIER_3_CATEGORIES

    def test_conflict_unresolvable_still_in_tier_3(self) -> None:
        # Pre-existing — included here for regression visibility.
        assert FAILURE_CATEGORY_CONFLICT_UNRESOLVABLE in TIER_3_CATEGORIES

    def test_bypassed_terminal_map_shape(self) -> None:
        # Issue #3455 added descriptive terminal phases for the
        # post-claude ``route_to_diagnoser`` arm in
        # agent-runner-entrypoint.sh. Each phase maps to its
        # FAILURE_CATEGORY so the diagnoser sweep picks them up.
        # ``ralph_not_ship`` is intentionally NOT here — it's handled
        # locally by the entrypoint.
        from dispatcher.daemon import (
            FAILURE_CATEGORY_FIX_CI_BLOCKED,
            FAILURE_CATEGORY_RALPH_AC_INFEASIBLE,
            FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE,
            FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE,
        )

        assert BYPASSED_TERMINAL_PHASES_TO_ROUTE == {
            "conflict_unresolvable": FAILURE_CATEGORY_CONFLICT_UNRESOLVABLE,
            "agent_runner_route_stub": FAILURE_CATEGORY_AGENT_RUNNER_ROUTE_STUB,
            "ralph_ac_infeasible": FAILURE_CATEGORY_RALPH_AC_INFEASIBLE,
            "summary_ac_infeasible": FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE,
            "fix_ci_blocked": FAILURE_CATEGORY_FIX_CI_BLOCKED,
            "verify_failed_post_merge": FAILURE_CATEGORY_VERIFY_FAILED_POST_MERGE,
        }


# --------------------------------------------------------------------------
# _build_bypassed_terminal_details
# --------------------------------------------------------------------------


class TestBuildBypassedTerminalDetails:
    def test_route_stub_carries_route_hint(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [
            # phase_outputs row for terminal phase ``agent_runner_route_stub``.
            ({"category": "agent_runner_route_stub", "reason": "ralph_not_ship: foo"},),
        ]
        d = _make_daemon(cur)

        details = d._build_bypassed_terminal_details(  # type: ignore[attr-defined]
            agent_id="abc",
            terminal_phase="agent_runner_route_stub",
            issue_number=42,
        )

        assert details["issue_number"] == 42
        assert details["terminal_phase"] == "agent_runner_route_stub"
        # The reason is carried through as both stderr_tail and route_hint.
        assert details["stderr_tail"] == "ralph_not_ship: foo"
        assert details["route_hint"] == "ralph_not_ship: foo"

    def test_conflict_unresolvable_pulls_fix_conflict_fields(self) -> None:
        cur = _FakeCursor()
        cur.fetch_queue = [
            # First fetch: the terminal-phase row.
            (
                {
                    "category": "conflict_unresolvable",
                    "reason": "fix_conflict skill returned unresolvable",
                },
            ),
            # Second fetch: the fix_conflict phase row.
            (
                {
                    "verdict": "unresolvable",
                    "conflict_files": ["packages/web/foo.tsx"],
                    "resolution_notes": "function rewritten on main",
                    "budget_exhausted": False,
                },
            ),
        ]
        d = _make_daemon(cur)

        details = d._build_bypassed_terminal_details(  # type: ignore[attr-defined]
            agent_id="abc",
            terminal_phase="conflict_unresolvable",
            issue_number=42,
        )

        assert details["terminal_phase"] == "conflict_unresolvable"
        assert details["stderr_tail"] == "fix_conflict skill returned unresolvable"
        assert details["conflict_files"] == ["packages/web/foo.tsx"]
        assert details["resolution_notes"] == "function rewritten on main"
        assert details["budget_exhausted"] is False
        assert details["verdict"] == "unresolvable"

    def test_db_error_returns_minimal_details(self) -> None:
        cur = _FakeCursor()

        # Force the first fetch to raise.
        def boom_execute(sql: str, params: Any = None) -> None:
            raise RuntimeError("db down")

        cur.execute = boom_execute  # type: ignore[method-assign]
        d = _make_daemon(cur)

        details = d._build_bypassed_terminal_details(  # type: ignore[attr-defined]
            agent_id="abc",
            terminal_phase="conflict_unresolvable",
            issue_number=42,
        )

        # Standard envelope is still present.
        assert details["issue_number"] == 42
        assert details["terminal_phase"] == "conflict_unresolvable"


# --------------------------------------------------------------------------
# _reap_completed_agent_tasks routing — bypassed terminals route through
# _handle_agent_failure now
# --------------------------------------------------------------------------


class TestReapRoutesBypassedTerminals:
    def _make_reaper_daemon(self) -> daemon.DispatcherDaemon:
        cur = _FakeCursor()
        d = _make_daemon(cur)
        # Wire enough config + ECS-client surface for the reaper.
        d._cfg = MagicMock(  # type: ignore[attr-defined]
            github_repo="judgemind/judgemind",
            ecs_cluster_arn="arn:aws:ecs:us-west-2:123:cluster/test",
        )
        d._ecs_client = MagicMock()  # type: ignore[attr-defined]
        return d

    def _agent_row(
        self, agent_id: str, issue_number: int, arn: str, phase: str, status: str
    ) -> tuple[Any, ...]:
        return (agent_id, issue_number, arn, phase, status)

    def test_agent_runner_route_stub_routes_through_handle_agent_failure(self) -> None:
        d = self._make_reaper_daemon()
        cur = d._main_conn._cursor  # type: ignore[attr-defined]
        agent_id = "agent-route-stub"
        issue_number = 99001
        arn = "arn:aws:ecs:us-west-2:123:task/test/route-stub"

        # 1. Initial SELECT for active agent rows.
        cur.fetchall_queue = [
            [
                self._agent_row(
                    agent_id, issue_number, arn, "agent_runner_route_stub", "failed"
                )
            ],
        ]
        # 2. Read agent status/phase post-STOPPED — in TERMINAL_AGENT_STATUSES.
        # 3. _build_bypassed_terminal_details fetches phase_outputs row.
        cur.fetch_queue = [
            ("failed", "agent_runner_route_stub"),  # _read_agent_status_phase
            (
                {
                    "category": "agent_runner_route_stub",
                    "reason": "ralph_not_ship: hint",
                },
            ),
        ]

        d._ecs_client.describe_tasks.return_value = {  # type: ignore[attr-defined]
            "tasks": [
                {
                    "taskArn": arn,
                    "lastStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "containers": [{"exitCode": 0}],
                }
            ]
        }

        with patch.object(d, "_handle_agent_failure") as mock_handle:
            with patch.object(d, "_clear_agent_task_arn") as mock_clear:
                summary = d._reap_completed_agent_tasks()

        mock_handle.assert_called_once()
        kwargs = mock_handle.call_args.kwargs
        assert kwargs["agent_id"] == agent_id
        assert kwargs["category"] == FAILURE_CATEGORY_AGENT_RUNNER_ROUTE_STUB
        assert kwargs["phase"] == "agent_runner_route_stub"
        assert kwargs["issue_number"] == issue_number
        # The route_hint from the phase_outputs row is carried through.
        assert kwargs["details"]["route_hint"] == "ralph_not_ship: hint"
        assert kwargs["details"]["stderr_tail"] == "ralph_not_ship: hint"
        assert kwargs["details"]["terminal_phase"] == "agent_runner_route_stub"

        # ARN clear ran so the next reap tick excludes the row.
        mock_clear.assert_called_once_with(
            agent_id=agent_id, issue_number=issue_number, branch="route_to_diagnoser"
        )

        # Counted as a failure reap, not a success reap.
        assert summary["reaped_failure"] == 1
        assert summary["reaped_success"] == 0

    def test_conflict_unresolvable_routes_through_handle_agent_failure(self) -> None:
        d = self._make_reaper_daemon()
        cur = d._main_conn._cursor  # type: ignore[attr-defined]
        agent_id = "agent-conflict"
        issue_number = 99002
        arn = "arn:aws:ecs:us-west-2:123:task/test/conflict"

        cur.fetchall_queue = [
            [
                self._agent_row(
                    agent_id, issue_number, arn, "conflict_unresolvable", "failed"
                )
            ],
        ]
        cur.fetch_queue = [
            ("failed", "conflict_unresolvable"),  # _read_agent_status_phase
            # phase_outputs for terminal phase
            (
                {
                    "category": "conflict_unresolvable",
                    "reason": "budget exhausted",
                },
            ),
            # phase_outputs for fix_conflict phase
            (
                {
                    "verdict": "unresolvable",
                    "conflict_files": ["a.py"],
                    "resolution_notes": "...",
                    "budget_exhausted": True,
                },
            ),
        ]

        d._ecs_client.describe_tasks.return_value = {  # type: ignore[attr-defined]
            "tasks": [
                {
                    "taskArn": arn,
                    "lastStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "containers": [{"exitCode": 0}],
                }
            ]
        }

        with patch.object(d, "_handle_agent_failure") as mock_handle:
            with patch.object(d, "_clear_agent_task_arn"):
                summary = d._reap_completed_agent_tasks()

        mock_handle.assert_called_once()
        kwargs = mock_handle.call_args.kwargs
        assert kwargs["category"] == FAILURE_CATEGORY_CONFLICT_UNRESOLVABLE
        assert kwargs["phase"] == "conflict_unresolvable"
        assert kwargs["details"]["conflict_files"] == ["a.py"]
        assert kwargs["details"]["budget_exhausted"] is True
        assert summary["reaped_failure"] == 1

    def test_non_bypass_terminal_phase_still_routes_to_success_finalize(self) -> None:
        """Terminal phase ``done`` (success) is unchanged by #3366."""
        d = self._make_reaper_daemon()
        cur = d._main_conn._cursor  # type: ignore[attr-defined]
        agent_id = "agent-success"
        issue_number = 99003
        arn = "arn:aws:ecs:us-west-2:123:task/test/success"

        cur.fetchall_queue = [
            [self._agent_row(agent_id, issue_number, arn, "done", "succeeded")],
        ]
        cur.fetch_queue = [
            ("succeeded", "done"),  # _read_agent_status_phase
        ]

        d._ecs_client.describe_tasks.return_value = {  # type: ignore[attr-defined]
            "tasks": [
                {
                    "taskArn": arn,
                    "lastStatus": "STOPPED",
                    "stopCode": "EssentialContainerExited",
                    "containers": [{"exitCode": 0}],
                }
            ]
        }

        with patch.object(d, "_handle_agent_failure") as mock_handle:
            with patch.object(d, "_reap_finalize_ecs_success") as mock_finalize:
                summary = d._reap_completed_agent_tasks()

        # Success path unchanged — no diagnoser routing.
        mock_handle.assert_not_called()
        mock_finalize.assert_called_once_with(agent_id, issue_number)
        assert summary["reaped_success"] == 1
        assert summary["reaped_failure"] == 0
