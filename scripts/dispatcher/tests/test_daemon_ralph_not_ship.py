"""Unit tests for ralph-originated non-SHIP verdict routing (#3054).

Covers:

* :meth:`daemon.DispatcherDaemon._run_ralph_phase` routes a non-SHIP,
  non-``AC_INFEASIBLE`` ralph verdict through
  :meth:`_handle_agent_failure` with
  ``category='ralph_not_ship'`` and
  ``details={verdict, block_reason, iterations_used, ...}`` — not via a
  direct :meth:`_mark_agent_terminal` call that bypassed the unified
  failure path (the pre-#3054 bug).
* :data:`daemon.FAILURE_CATEGORY_RALPH_NOT_SHIP` is in
  :data:`daemon.TIER_3_CATEGORIES` and NOT in
  :data:`daemon.AUTO_RETRY_CATEGORIES`, so the diagnoser candidate scan
  picks it up on first occurrence with no mechanical retry.
* The ``ralph_not_ship`` failure row is lifted by
  :meth:`_find_diagnoser_candidates` with ``tier == 3`` (mirrors the
  selection helper pattern used in #3042 / #3010 tests).
* Regression guards: SHIP still advances to summary; AC_INFEASIBLE does
  not flow through the new ralph_not_ship branch.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Install a psycopg stub whose ``errors.UniqueViolation`` is a real
# Exception subclass — matches the pattern in earlier phase test files.
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
# Shared fakes (mirror test_daemon_ralph_ac_infeasible.py / test_daemon_phase3d.py)
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
    logger = logging.getLogger(f"dispatcher.test.ralph_not_ship.{id(tmp_path)}")
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


def _patch_ralph_helpers(
    d: daemon.DispatcherDaemon, *, ralph_output: dict[str, Any]
) -> None:
    """Patch the helpers _run_ralph_phase calls so the test doesn't need
    real subprocesses, DB, or filesystem state."""
    d._update_agent_phase = MagicMock()  # type: ignore[method-assign]
    d._materialize_prior_attempts = MagicMock(return_value=0)  # type: ignore[method-assign]
    d._write_phase_input = MagicMock()  # type: ignore[method-assign]
    d._run_subprocess_or_fail = MagicMock(return_value=0)  # type: ignore[method-assign]
    d._read_phase_output = MagicMock(return_value=ralph_output)  # type: ignore[method-assign]
    d._persist_phase_output = MagicMock()  # type: ignore[method-assign]
    d._read_full_phase_log = MagicMock(return_value="")  # type: ignore[method-assign]
    d._parse_phase_usage = MagicMock(return_value=None)  # type: ignore[method-assign]
    # _handle_agent_failure is the new routing path under test.
    d._handle_agent_failure = MagicMock()  # type: ignore[method-assign]
    # _mark_agent_terminal / _write_failure are still stubbed so the
    # regression branches that don't route through _handle_agent_failure
    # (SHIP, AC_INFEASIBLE) can still be asserted.
    d._mark_agent_terminal = MagicMock()  # type: ignore[method-assign]
    d._write_failure = MagicMock()  # type: ignore[method-assign]


# --------------------------------------------------------------------------
# Tier registration (AC #1 tier-set membership)
# --------------------------------------------------------------------------


class TestTierRegistration:
    def test_ralph_not_ship_is_tier_3(self) -> None:
        """``ralph_not_ship`` diagnoses on first occurrence — no retry.

        Mirrors :data:`FAILURE_CATEGORY_RALPH_AC_INFEASIBLE`: ralph can't
        ship is qualitatively "implementation blocked" with no
        mechanical retry that reliably fixes it. The diagnoser picks
        ``block_on_existing_task`` / ``file_prerequisite_task`` /
        ``block_and_comment`` based on the block_reason.
        """
        assert daemon.FAILURE_CATEGORY_RALPH_NOT_SHIP in daemon.TIER_3_CATEGORIES

    def test_ralph_not_ship_not_in_auto_retry(self) -> None:
        """Ralph-not-ship is NOT auto-retried — the daemon's pre-#3054
        re-pick-on-cooldown bleed (same issue, same block_reason, 3x in
        a row on 2026-04-23 for #2630 / #2633) was precisely the bug."""
        assert (
            daemon.FAILURE_CATEGORY_RALPH_NOT_SHIP not in daemon.AUTO_RETRY_CATEGORIES
        )

    def test_ralph_not_ship_not_in_tier2_buckets(self) -> None:
        """Tier buckets must be disjoint — the candidate scanner tier
        assignment branches on exclusive membership (see
        :meth:`_find_diagnoser_candidates`).
        """
        assert (
            daemon.FAILURE_CATEGORY_RALPH_NOT_SHIP
            not in daemon.TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )
        assert (
            daemon.FAILURE_CATEGORY_RALPH_NOT_SHIP
            not in daemon.TIER_2_RECURRENCE_CATEGORIES
        )


# --------------------------------------------------------------------------
# _run_ralph_phase behavior on verdict != SHIP / != AC_INFEASIBLE (AC #1)
# --------------------------------------------------------------------------


class TestRunRalphPhaseNotShip:
    def test_blocked_verdict_routes_through_handle_agent_failure(
        self, tmp_path: Path
    ) -> None:
        """BLOCKED verdict (worker-STUCK twice or max_iterations with a
        block_reason) writes a ``ralph_not_ship`` failure row via the
        unified handler, carrying the block_reason + iterations_used in
        ``details`` so the diagnoser can reason over it.
        """
        d, _conn, handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_ralph_helpers(
            d,
            ralph_output={
                "verdict": "BLOCKED",
                "iterations_used": 5,
                "block_reason": (
                    "PAT missing `workflow` scope — push rejected on any "
                    "PR that touches .github/workflows/**. Need operator "
                    "to update the dispatcher-agent PAT in Secrets Manager."
                ),
                "changed_files": [],
                "summary": "ralph hit the PAT scope wall repeatedly.",
            },
        )

        ok = d._run_ralph_phase("agent-blocked", 3054, worktree)

        # Ralph short-circuits — summary phase not advanced to.
        assert ok is False

        # _handle_agent_failure was called exactly once with the right shape.
        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["agent_id"] == "agent-blocked"
        assert kwargs["phase"] == "ralph"
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_RALPH_NOT_SHIP
        assert kwargs["issue_number"] == 3054
        # exit_code comes through as 0 from the stubbed subprocess call.
        assert kwargs["exit_code"] == 0
        details = kwargs["details"]
        assert details["verdict"] == "BLOCKED"
        assert details["iterations_used"] == 5
        assert "PAT missing `workflow` scope" in details["block_reason"]
        assert details["agent_id"] == "agent-blocked"
        assert details["issue_number"] == 3054

        # Direct _mark_agent_terminal was NOT called — the unified
        # handler owns that (pre-#3054 bypass fixed).
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]
        # And the direct _write_failure shortcut was not used either.
        d._write_failure.assert_not_called()  # type: ignore[union-attr]

        # Observability — ralph_not_ship event still logged with the new
        # iterations_used field (so CloudWatch queries aren't regressed).
        events = handler.events("ralph_not_ship")
        assert events
        record = events[0]
        assert getattr(record, "verdict", None) == "BLOCKED"
        assert getattr(record, "iterations_used", None) == 5
        assert "PAT" in getattr(record, "block_reason", "")

    def test_revise_verdict_also_routes_through_handle_agent_failure(
        self, tmp_path: Path
    ) -> None:
        """REVISE (or any non-SHIP, non-AC_INFEASIBLE verdict) writes a
        ``ralph_not_ship`` failure row. Covers the REVISE-exhausted
        branch where ralph used its full iteration budget without
        resolving reviewer objections.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_ralph_helpers(
            d,
            ralph_output={
                "verdict": "REVISE",
                "iterations_used": 5,
                "block_reason": "reviewer flagged 3 critical issues on iteration 5",
                "changed_files": ["scripts/dispatcher/daemon.py"],
                "summary": "ran out of iterations before reviewer agreed.",
            },
        )

        ok = d._run_ralph_phase("agent-revise", 2630, worktree)

        assert ok is False
        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        kwargs = d._handle_agent_failure.call_args.kwargs  # type: ignore[union-attr]
        assert kwargs["category"] == daemon.FAILURE_CATEGORY_RALPH_NOT_SHIP
        assert kwargs["details"]["verdict"] == "REVISE"
        assert kwargs["details"]["iterations_used"] == 5
        assert "reviewer flagged" in kwargs["details"]["block_reason"]

    def test_block_reason_absent_still_routes_without_crash(
        self, tmp_path: Path
    ) -> None:
        """Defensive parsing — a missing ``block_reason`` must not crash
        the daemon; route through with ``block_reason=None``. The
        diagnoser can still escalate from the verdict alone.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_ralph_helpers(
            d,
            ralph_output={
                "verdict": "STUCK",
                # no block_reason, no iterations_used — skill contract
                # violation, but the daemon must not crash.
                "changed_files": [],
                "summary": "worker reported STUCK twice.",
            },
        )

        ok = d._run_ralph_phase("agent-stuck", 2629, worktree)

        assert ok is False
        d._handle_agent_failure.assert_called_once()  # type: ignore[union-attr]
        details = d._handle_agent_failure.call_args.kwargs["details"]  # type: ignore[union-attr]
        assert details["verdict"] == "STUCK"
        assert details["block_reason"] is None
        assert details["iterations_used"] is None


# --------------------------------------------------------------------------
# Regression guards (AC #3)
# --------------------------------------------------------------------------


class TestRegressionGuards:
    def test_ship_verdict_still_advances_to_summary(self, tmp_path: Path) -> None:
        """SHIP path must not be affected — no failure row written, no
        terminal mark, ralph output stashed for the summary phase. This
        is the existing ``test_daemon_ralph_patches.py`` /
        ``test_daemon_phase3d.py`` guarantee we must preserve.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # _capture_and_persist_ralph_patch does real disk work — stub it.
        d._capture_and_persist_ralph_patch = MagicMock()  # type: ignore[method-assign]

        _patch_ralph_helpers(
            d,
            ralph_output={
                "verdict": "SHIP",
                "iterations_used": 1,
                "block_reason": None,
                "changed_files": ["docs/example.md"],
                "summary": "implemented cleanly.",
            },
        )

        ok = d._run_ralph_phase("agent-ship", 3054, worktree)

        assert ok is True
        # Neither failure path was taken.
        d._handle_agent_failure.assert_not_called()  # type: ignore[union-attr]
        d._write_failure.assert_not_called()  # type: ignore[union-attr]
        d._mark_agent_terminal.assert_not_called()  # type: ignore[union-attr]
        # SHIP path stashes output for summary.
        assert d._agent_ralph_output is not None
        assert d._agent_ralph_output.get("verdict") == "SHIP"

    def test_ac_infeasible_verdict_still_uses_ac_infeasible_path(
        self, tmp_path: Path
    ) -> None:
        """AC_INFEASIBLE must NOT be absorbed by the new ralph_not_ship
        branch — it has its own category, its own details shape
        (``infeasible_acs`` list), and its own diagnoser context bundle
        (``issue_acceptance_criteria``). Locking this down prevents a
        future refactor from collapsing the two paths accidentally.
        """
        d, _conn, _handler = _make_daemon(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        _patch_ralph_helpers(
            d,
            ralph_output={
                "verdict": "AC_INFEASIBLE",
                "iterations_used": 2,
                "block_reason": None,
                "changed_files": [],
                "infeasible_acs": [
                    {"index": 1, "evidence": "nonexistent --court flag"},
                ],
                "summary": "1 AC structurally infeasible.",
            },
        )

        ok = d._run_ralph_phase("agent-ac-inf", 3054, worktree)

        assert ok is False
        # AC_INFEASIBLE takes the dedicated _write_failure path — NOT the
        # new _handle_agent_failure ralph_not_ship path.
        d._handle_agent_failure.assert_not_called()  # type: ignore[union-attr]
        d._write_failure.assert_called_once()  # type: ignore[union-attr]
        write_kwargs = d._write_failure.call_args.kwargs  # type: ignore[union-attr]
        assert write_kwargs["category"] == daemon.FAILURE_CATEGORY_RALPH_AC_INFEASIBLE
        # _mark_agent_terminal is still called in the AC_INFEASIBLE branch
        # (the dedicated path writes the failure row then marks terminal).
        d._mark_agent_terminal.assert_called_once()  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# _find_diagnoser_candidates picks up the ralph_not_ship failure row (AC #2)
# --------------------------------------------------------------------------


class TestDiagnoserCandidatePickup:
    def test_ralph_not_ship_row_is_tier_3_candidate(self, tmp_path: Path) -> None:
        """A ``dispatcher.failures(category='ralph_not_ship')`` row is
        lifted by :meth:`_find_diagnoser_candidates` with ``tier == 3``
        and the ``issue_number`` threaded through from ``details``. This
        is the selection-helper coverage the issue calls out (mirrors
        the tier-3 tests in :mod:`test_daemon_phase3d`).
        """
        d, conn, _handler = _make_daemon(tmp_path)

        failure_row = (
            777,  # failure_id
            "agent-ralph-not-ship",  # agent_id
            daemon.FAILURE_CATEGORY_RALPH_NOT_SHIP,  # category
            {
                "issue_number": 2633,
                "verdict": "BLOCKED",
                "block_reason": "PAT scope missing",
                "iterations_used": 5,
                "phase": "ralph",
                "exit_code": 0,
                "stderr_tail": "",
            },
            datetime(2026, 4, 23, 12, 13, 11, tzinfo=UTC),  # ts
        )
        conn.cursor_instance.fetchall_queue = [[failure_row]]

        candidates = d._find_diagnoser_candidates()

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["failure_id"] == 777
        assert cand["agent_id"] == "agent-ralph-not-ship"
        assert cand["category"] == daemon.FAILURE_CATEGORY_RALPH_NOT_SHIP
        assert cand["tier"] == 3
        assert cand["issue_number"] == 2633
        # details JSONB round-tripped through the scanner.
        assert cand["details"]["verdict"] == "BLOCKED"
        assert cand["details"]["block_reason"] == "PAT scope missing"
        assert cand["details"]["iterations_used"] == 5
