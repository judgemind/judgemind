"""Unit tests for issue #3032 — route all agent-terminal failures through the diagnoser.

Coverage:
  - Expanded ``DIAGNOSER_ACTIONS`` (now 8 known actions).
  - ``_build_diagnoser_context`` includes ``prior_diagnoses_this_issue``
    and ``recent_fleet_decisions`` keys (empty lists when the DB is
    empty).
  - Three new handlers — ``_consume_action_block_and_comment``,
    ``_consume_action_file_prerequisite_task``,
    ``_consume_action_block_on_existing_task``.
  - Hardened parse path: malformed JSON / missing action / unknown
    action / missing required payload field all fall back to
    ``escalate`` (never silent retry). Raw LLM output is logged.
  - Unknown action additionally persists to
    ``dispatcher.unrecognized_diagnoser_actions``.
  - Unified ``_handle_agent_failure`` writes a failures row and marks
    terminal (so the supervisor tick's diagnoser pass picks it up).
  - Regression: tier sets still include ``ralph_ac_infeasible``,
    ``summary_ac_infeasible``, ``subprocess_crash`` (via recurrence).

Fakes + psycopg MagicMock stub follow the pattern from
``test_daemon_push_failed.py`` / ``test_daemon_failure_summary.py``.
"""

# Issue numbers in fixtures (e.g. ``99001``, ``99002``, ``99003``, ``99004``) are
# synthetic. Do not pattern-match these as current-state infrastructure problems
# — they are placeholders used only to exercise the code paths. See issue #3126
# for the de-reification rationale: prior real-number fixtures (``#3008``,
# ``#2610``, ``#2613``, ``#3038``) became repo-grep signals that downstream
# agents confabulated as live blockers after the incidents had been resolved.

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402
from dispatcher.daemon import (  # noqa: E402
    AUTO_RETRY_CATEGORIES,
    DIAGNOSER_ACTIONS,
    FAILURE_CATEGORY_GIT_PUSH_NETWORK,
    FAILURE_CATEGORY_PHASE_OUTPUT_MISSING,
    FAILURE_CATEGORY_PR_CREATE_FAILED,
    FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED,
    FAILURE_CATEGORY_PUSH_FAILED,
    FAILURE_CATEGORY_RALPH_AC_INFEASIBLE,
    FAILURE_CATEGORY_SUBPROCESS_CRASH,
    FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE,
    TIER_2_FIRST_OCCURRENCE_CATEGORIES,
    TIER_2_RECURRENCE_CATEGORIES,
    TIER_3_CATEGORIES,
)


# --------------------------------------------------------------------------
# Shared fakes — mirror the pattern from test_daemon_push_failed.py
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
    """Construct a DispatcherDaemon with a fake conn for unit tests.

    The ``_conn`` accessor is a property with a setter that writes to
    ``_main_conn``. We bypass ``__init__`` (no DB, no config), then
    manually populate the fields the unit-under-test reads. The
    ``_thread_state`` attribute is a ``threading.local`` instance in
    real code; a bare ``MagicMock`` suffices here since the property
    falls back to ``_main_conn`` when ``_thread_state.conn`` is None.
    """
    import threading

    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    d._main_conn = _FakeConn(cursor)  # type: ignore[attr-defined]
    d._cfg = MagicMock(github_repo="judgemind/judgemind")  # type: ignore[attr-defined]
    d._log = logging.getLogger("test.daemon")  # type: ignore[attr-defined]
    d._run_id = "test-run"  # type: ignore[attr-defined]
    return d


# --------------------------------------------------------------------------
# AC#4 — DIAGNOSER_ACTIONS is now 8 (open menu, issue #3032)
# --------------------------------------------------------------------------


class TestDiagnoserActionsExpanded:
    def test_eight_known_actions(self) -> None:
        assert DIAGNOSER_ACTIONS == frozenset(
            {
                "retry",
                "retry_with_hint",
                "reissue",
                "escalate",
                "close",
                "block_and_comment",
                "file_prerequisite_task",
                "block_on_existing_task",
            }
        )

    def test_original_five_still_present(self) -> None:
        for action in ("retry", "retry_with_hint", "reissue", "escalate", "close"):
            assert action in DIAGNOSER_ACTIONS

    def test_three_new_actions(self) -> None:
        assert "block_and_comment" in DIAGNOSER_ACTIONS
        assert "file_prerequisite_task" in DIAGNOSER_ACTIONS
        assert "block_on_existing_task" in DIAGNOSER_ACTIONS


# --------------------------------------------------------------------------
# AC#2 — tier sets include the new categories routed via _handle_agent_failure
# --------------------------------------------------------------------------


class TestTierSetsIncludeNewCategories:
    def test_push_categories_in_tier2_first_occurrence(self) -> None:
        assert FAILURE_CATEGORY_PUSH_FAILED in TIER_2_FIRST_OCCURRENCE_CATEGORIES
        assert (
            FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED
            in TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )
        assert FAILURE_CATEGORY_GIT_PUSH_NETWORK in TIER_2_FIRST_OCCURRENCE_CATEGORIES

    def test_pr_create_failed_in_tier2_first_occurrence(self) -> None:
        assert FAILURE_CATEGORY_PR_CREATE_FAILED in TIER_2_FIRST_OCCURRENCE_CATEGORIES

    def test_phase_output_missing_in_tier2_first_occurrence(self) -> None:
        assert (
            FAILURE_CATEGORY_PHASE_OUTPUT_MISSING in TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )

    def test_phase_output_fetch_failed_in_tier2_first_occurrence(self) -> None:
        """#3385 — DB-layer fetch errors route through the diagnoser on first
        occurrence so the diagnoser can pick retry vs escalate based on
        whether the failure is transient or persistent.
        """
        assert (
            daemon.FAILURE_CATEGORY_PHASE_OUTPUT_FETCH_FAILED
            in TIER_2_FIRST_OCCURRENCE_CATEGORIES
        )

    def test_regression_ac_infeasible_categories_still_tier3(self) -> None:
        assert FAILURE_CATEGORY_RALPH_AC_INFEASIBLE in TIER_3_CATEGORIES
        assert FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE in TIER_3_CATEGORIES

    def test_conflict_unresolvable_in_tier3(self) -> None:
        """#3225 — fix_conflict terminal routes through the diagnoser."""
        assert daemon.FAILURE_CATEGORY_CONFLICT_UNRESOLVABLE in TIER_3_CATEGORIES
        # Not in tier-1 auto-retry (we don't want the mechanical retry
        # path to swallow it — diagnoser picks AC_INFEASIBLE /
        # retry_with_hint based on resolution_notes).
        assert (
            daemon.FAILURE_CATEGORY_CONFLICT_UNRESOLVABLE not in AUTO_RETRY_CATEGORIES
        )

    def test_regression_subprocess_crash_still_tier2_recurrence(self) -> None:
        assert FAILURE_CATEGORY_SUBPROCESS_CRASH in TIER_2_RECURRENCE_CATEGORIES

    def test_push_categories_removed_from_auto_retry(self) -> None:
        # Deliberate behavior change (#3032) — the diagnoser now owns
        # the retry-vs-escalate decision for push failures.
        assert FAILURE_CATEGORY_PUSH_FAILED not in AUTO_RETRY_CATEGORIES
        assert FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED not in AUTO_RETRY_CATEGORIES
        assert FAILURE_CATEGORY_GIT_PUSH_NETWORK not in AUTO_RETRY_CATEGORIES

    def test_tier_sets_still_disjoint(self) -> None:
        # Mutual exclusion is a required invariant of the candidate
        # scanner — a category in two sets would be double-tiered.
        assert (
            TIER_2_RECURRENCE_CATEGORIES & TIER_2_FIRST_OCCURRENCE_CATEGORIES
            == frozenset()
        )
        assert TIER_2_RECURRENCE_CATEGORIES & TIER_3_CATEGORIES == frozenset()
        assert TIER_2_FIRST_OCCURRENCE_CATEGORIES & TIER_3_CATEGORIES == frozenset()


# --------------------------------------------------------------------------
# AC#1 — _handle_agent_failure writes failures row + marks terminal
# --------------------------------------------------------------------------


class TestHandleAgentFailure:
    def test_writes_failure_row_and_marks_terminal(self) -> None:
        d = _make_daemon()
        with (
            patch.object(d, "_write_failure") as mock_write,
            patch.object(d, "_mark_agent_terminal") as mock_mark,
        ):
            d._handle_agent_failure(
                agent_id="agent-1",
                phase="push_and_pr",
                category=FAILURE_CATEGORY_PUSH_FAILED,
                stderr_tail="refusing to allow a Personal Access Token",
                exit_code=1,
                details={"branch": "agent/abc123"},
                issue_number=99001,
            )

        # _write_failure was called with the right shape.
        mock_write.assert_called_once()
        kwargs = mock_write.call_args.kwargs
        assert kwargs["agent_id"] == "agent-1"
        assert kwargs["category"] == FAILURE_CATEGORY_PUSH_FAILED
        assert kwargs["detected_by"] == "scheduler"
        details = kwargs["details"]
        assert details["phase"] == "push_and_pr"
        assert details["stderr_tail"] == "refusing to allow a Personal Access Token"
        assert details["exit_code"] == 1
        assert details["branch"] == "agent/abc123"

        # _mark_agent_terminal called with failed status + phase.
        mock_mark.assert_called_once()
        args, kwargs = mock_mark.call_args
        assert args[0] == "agent-1"
        assert kwargs["status"] == "failed"
        assert kwargs["phase"] == "push_and_pr"
        assert kwargs["exit_code"] == 1
        assert kwargs["issue_number"] == 99001

    def test_details_defaults_to_empty_dict(self) -> None:
        d = _make_daemon()
        with (
            patch.object(d, "_write_failure") as mock_write,
            patch.object(d, "_mark_agent_terminal"),
        ):
            d._handle_agent_failure(
                agent_id="agent-2",
                phase="push_and_pr",
                category=FAILURE_CATEGORY_PR_CREATE_FAILED,
                stderr_tail="HTTP 422",
                exit_code=1,
            )
        details = mock_write.call_args.kwargs["details"]
        # Canonical envelope always present even with no caller details.
        assert details["phase"] == "push_and_pr"
        assert details["stderr_tail"] == "HTTP 422"
        assert details["exit_code"] == 1


# --------------------------------------------------------------------------
# AC#3 — context bundle includes prior_diagnoses_this_issue + recent_fleet_decisions
# --------------------------------------------------------------------------


class TestContextBundleExtensions:
    def test_bundle_always_includes_new_keys(self) -> None:
        """Issue #3032 — both keys are always present in the bundle.

        When the DB is empty both come back as empty lists, but the
        keys are guaranteed — so downstream consumers (and tests) can
        count on stable shape.
        """
        # A minimal candidate for a push_failed first-occurrence.
        candidate = {
            "failure_id": 42,
            "agent_id": "agent-ctx",
            "category": FAILURE_CATEGORY_PUSH_FAILED,
            "tier": 2,
            "issue_number": 99001,
            "details": {"branch": "agent/abc"},
            "failure_ts": None,
        }

        cursor = _FakeCursor()
        # 1st fetchone: agents row — (issue_number, worktree_path, pr_number).
        cursor.fetch_queue.append((99001, "", None))
        # fetchall queue — phase transitions, prior failures, prior
        # diagnoses, recent fleet decisions. (Some sub-queries in the
        # builder call fetchone; others fetchall. Pre-seed both.)
        cursor.fetchall_queue.extend(
            [
                [],  # phase_transitions
                [],  # prior_failures
                [],  # prior_diagnoses_this_issue
                [],  # recent_fleet_decisions
            ]
        )

        d = _make_daemon(cursor)
        # Short-circuit the gh issue view subprocess with an empty
        # issue fetch — we're only checking the bundle shape.
        with patch("dispatcher.daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            bundle = d._build_diagnoser_context(candidate)

        assert "prior_diagnoses_this_issue" in bundle
        assert "recent_fleet_decisions" in bundle
        assert bundle["prior_diagnoses_this_issue"] == []
        assert bundle["recent_fleet_decisions"] == []

    def test_prior_diagnoses_this_issue_populated(self) -> None:
        """When the DB has prior diagnoses on the same issue, they land in the bundle."""
        candidate = {
            "failure_id": 42,
            "agent_id": "agent-xyz",
            "category": FAILURE_CATEGORY_PUSH_FAILED,
            "tier": 2,
            "issue_number": 99001,
            "details": {},
            "failure_ts": None,
        }
        cursor = _FakeCursor()
        # agents row
        cursor.fetch_queue.append((99001, "", None))
        # Phase transitions, prior failures, prior diagnoses, recent fleet decisions.
        # prior_diagnoses_this_issue returns one row.
        from datetime import datetime, timezone

        completed_at = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
        cursor.fetchall_queue.extend(
            [
                [],  # phase_transitions
                [],  # prior_failures
                [
                    (
                        100,
                        "push_failed",
                        {"action": "retry", "reasoning": "transient"},
                        completed_at,
                    )
                ],  # prior_diagnoses_this_issue
                [],  # recent_fleet_decisions
            ]
        )
        d = _make_daemon(cursor)
        with patch("dispatcher.daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            bundle = d._build_diagnoser_context(candidate)

        assert len(bundle["prior_diagnoses_this_issue"]) == 1
        row = bundle["prior_diagnoses_this_issue"][0]
        assert row["diagnosis_id"] == 100
        assert row["failure_category"] == "push_failed"
        assert row["recommendation"] == {"action": "retry", "reasoning": "transient"}
        assert row["completed_at"] == completed_at.isoformat()

    def test_recent_fleet_decisions_populated(self) -> None:
        """Recent fleet decisions (last 6h, cap 20) land as action+reasoning tuples."""
        candidate = {
            "failure_id": 42,
            "agent_id": "agent-xyz",
            "category": FAILURE_CATEGORY_PUSH_FAILED,
            "tier": 2,
            "issue_number": 99001,
            "details": {},
            "failure_ts": None,
        }
        cursor = _FakeCursor()
        cursor.fetch_queue.append((99001, "", None))
        from datetime import datetime, timezone

        t = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
        cursor.fetchall_queue.extend(
            [
                [],
                [],
                [],
                [
                    (
                        200,
                        "agent-a",
                        99002,
                        "pre_push_hook_rejected",
                        {
                            "action": "block_and_comment",
                            "reasoning": "PAT scope missing",
                        },
                        t,
                    ),
                    (
                        201,
                        "agent-b",
                        99001,
                        "pre_push_hook_rejected",
                        {
                            "action": "file_prerequisite_task",
                            "reasoning": "Fleet-wide PAT",
                        },
                        t,
                    ),
                ],
            ]
        )
        d = _make_daemon(cursor)
        with patch("dispatcher.daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            bundle = d._build_diagnoser_context(candidate)

        decisions = bundle["recent_fleet_decisions"]
        assert len(decisions) == 2
        assert decisions[0]["action"] == "block_and_comment"
        assert decisions[0]["failure_category"] == "pre_push_hook_rejected"
        assert decisions[0]["issue_number"] == 99002
        assert decisions[1]["action"] == "file_prerequisite_task"
        assert decisions[1]["issue_number"] == 99001


# --------------------------------------------------------------------------
# Issue #3057 — recent_fleet_decisions cap is configurable (anchor-bias defense)
# --------------------------------------------------------------------------


class TestFleetDecisionsCapConfigurable:
    """Issue #3057 — ``recent_fleet_decisions`` defaults to LIMIT 3 instead
    of the prior LIMIT 20, and the cap is tunable via
    ``dispatcher.config.diagnoser_fleet_decisions_cap``.

    The anchor-bias failure: on 2026-04-23 the Opus diagnoser hallucinated
    a PAT-scope cascade push-rejection on a coverage-floor failure after
    9 identical ``block_on_existing_task → #99003`` decisions populated the
    fleet-decisions bundle. Reducing to 3 preserves the fleet-wide signal
    without letting any single pattern dominate.
    """

    def _build_candidate(self) -> dict[str, Any]:
        return {
            "failure_id": 42,
            "agent_id": "agent-cap",
            "category": FAILURE_CATEGORY_PUSH_FAILED,
            "tier": 2,
            "issue_number": 99001,
            "details": {},
            "failure_ts": None,
        }

    def test_default_cap_is_three(self) -> None:
        """With no ``diagnoser_fleet_decisions_cap`` row, the LIMIT parameter
        passed to the SQL is 3 (the default)."""
        cursor = _FakeCursor()
        # agents row
        cursor.fetch_queue.append((99001, "", None))
        # _cb_config_int fetchone — no row means default (3).
        cursor.fetch_queue.append(None)
        # Phase transitions, prior failures, prior diagnoses, recent fleet decisions.
        cursor.fetchall_queue.extend(
            [
                [],  # phase_transitions
                [],  # prior_failures
                [],  # prior_diagnoses_this_issue
                [],  # recent_fleet_decisions
            ]
        )
        d = _make_daemon(cursor)
        with patch("dispatcher.daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            d._build_diagnoser_context(self._build_candidate())

        # Find the SELECT that builds recent_fleet_decisions — the one
        # joining dispatcher.diagnoses / agents / failures with the 6-hour
        # window. Its params tuple should be ``(3,)`` — the default cap.
        fleet_selects = [
            (sql, params)
            for (sql, params) in cursor.executed
            if "FROM dispatcher.diagnoses d" in sql
            and "JOIN dispatcher.agents" in sql
            and "JOIN dispatcher.failures" in sql
            and "interval '6 hours'" in sql
        ]
        assert len(fleet_selects) == 1, (
            f"expected exactly one fleet-decisions SELECT, got {len(fleet_selects)}"
        )
        _sql, params = fleet_selects[0]
        assert params == (3,), f"default cap should be 3; got {params!r}. SQL: {_sql!r}"

    def test_cap_honors_dispatcher_config_override(self) -> None:
        """When ``dispatcher.config.diagnoser_fleet_decisions_cap`` is set
        to a different positive integer, the LIMIT parameter reflects it."""
        cursor = _FakeCursor()
        cursor.fetch_queue.append((99001, "", None))  # agents row
        # _cb_config_int fetchone — returns a row with the override value.
        # Shape: (value,) where value is the JSON-serializable cap.
        cursor.fetch_queue.append((5,))
        cursor.fetchall_queue.extend(
            [
                [],  # phase_transitions
                [],  # prior_failures
                [],  # prior_diagnoses_this_issue
                [],  # recent_fleet_decisions
            ]
        )
        d = _make_daemon(cursor)
        with patch("dispatcher.daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            d._build_diagnoser_context(self._build_candidate())

        fleet_selects = [
            (sql, params)
            for (sql, params) in cursor.executed
            if "FROM dispatcher.diagnoses d" in sql
            and "JOIN dispatcher.agents" in sql
            and "JOIN dispatcher.failures" in sql
            and "interval '6 hours'" in sql
        ]
        assert len(fleet_selects) == 1
        _sql, params = fleet_selects[0]
        assert params == (5,), (
            f"override cap should be 5; got {params!r}. SQL: {_sql!r}"
        )

    def test_cap_uses_limit_parameter_not_hardcoded_literal(self) -> None:
        """Regression guard: the SELECT must use a bound ``%s`` parameter
        for LIMIT, not a hardcoded literal like ``LIMIT 20`` (which was
        the pre-#3057 behavior that produced the anchor-bias failure)."""
        cursor = _FakeCursor()
        cursor.fetch_queue.append((99001, "", None))
        cursor.fetch_queue.append(None)  # _cb_config_int → default
        cursor.fetchall_queue.extend([[], [], [], []])
        d = _make_daemon(cursor)
        with patch("dispatcher.daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            d._build_diagnoser_context(self._build_candidate())

        fleet_selects = [
            sql
            for (sql, _params) in cursor.executed
            if "FROM dispatcher.diagnoses d" in sql
            and "JOIN dispatcher.agents" in sql
            and "interval '6 hours'" in sql
        ]
        assert len(fleet_selects) == 1
        sql = fleet_selects[0]
        # The old behavior baked 20 into the SQL string. The new behavior
        # must parameterize the cap.
        assert "LIMIT 20" not in sql, (
            "LIMIT 20 literal still present — the #3057 fix should use "
            "``LIMIT %s`` with the configurable cap"
        )
        assert "LIMIT %s" in sql, (
            "expected ``LIMIT %s`` in fleet-decisions SELECT (issue #3057)"
        )

    def test_simulated_pat_cascade_context_does_not_crash_with_new_cap(
        self,
    ) -> None:
        """Golden regression test for the anchor-bias bug (issue #3057).

        Simulates the exact failure context from 2026-04-23: a failure
        with a ``FAILED: coverage floor`` stderr arrives while the
        fleet-decisions queue is dominated by ``block_on_existing_task →
        #99003`` PAT-cascade decisions. The daemon-side context bundler
        should now cap at 3 entries (down from 20) — limiting the LLM's
        exposure to the PAT pattern.

        The full anti-hallucination behavior is LLM-side (skill §Step 1
        mandates a verbatim stderr quote before consulting priors), so
        this test locks the structural half: the context bundle the
        daemon hands the skill contains at most 3 fleet decisions, even
        when the DB has more. The SQL ``LIMIT 3`` enforces the cap
        server-side; this test asserts the Python-side bundle respects
        it when the DB honors the LIMIT.
        """
        from datetime import datetime, timezone

        cursor = _FakeCursor()
        cursor.fetch_queue.append((99004, "", None))  # agents row for #99004
        cursor.fetch_queue.append(None)  # _cb_config_int → default 3
        t = datetime(2026, 4, 23, 6, 59, 0, tzinfo=timezone.utc)

        # Simulate the DB-respected LIMIT 3 — three PAT-cascade decisions.
        # (In real life, 9+ existed in the diagnoses table; the SQL
        # LIMIT 3 means only the 3 most-recent come back.)
        pat_decisions = [
            (
                300 + i,
                f"agent-{i:02d}",
                99001 + i,
                "pre_push_hook_rejected",
                {
                    "action": "block_on_existing_task",
                    "reasoning": (
                        "PAT-scope cascade — refusing to allow a Personal "
                        "Access Token to create or update workflow "
                        ".github/workflows/.* without workflow scope. "
                        "Tracked at #99003."
                    ),
                    "blocker_issue_number": 99003,
                },
                t,
            )
            for i in range(3)
        ]
        cursor.fetchall_queue.extend(
            [
                [],  # phase_transitions
                [],  # prior_failures
                [],  # prior_diagnoses_this_issue
                pat_decisions,  # recent_fleet_decisions (capped at 3)
            ]
        )
        candidate = {
            "failure_id": 99,
            "agent_id": "6d4029f0",
            # A coverage-floor failure comes in as pre_push_hook_rejected
            # category (the pre-push hook is what aborted).
            "category": FAILURE_CATEGORY_PRE_PUSH_HOOK_REJECTED,
            "tier": 2,
            "issue_number": 99004,
            "details": {
                "stderr_tail": (
                    "pre-push: checking coverage floor for 'scraper-framework'...\n"
                    "  FAILED: coverage floor for scraper-framework\n"
                    "  Last 20 lines of output:\n"
                    "    FAIL: packages/scraper-framework: coverage dropped "
                    "80.0% -> 68.6% (floor violation)\n"
                    "pre-push: 1 check(s) failed. Push aborted."
                )
            },
            "failure_ts": None,
        }
        d = _make_daemon(cursor)
        with patch("dispatcher.daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            bundle = d._build_diagnoser_context(candidate)

        # Structural assertion: the bundle's recent_fleet_decisions has
        # at most 3 entries (SQL LIMIT 3 with default cap).
        assert len(bundle["recent_fleet_decisions"]) <= 3
        assert len(bundle["recent_fleet_decisions"]) == 3

        # The fleet-decisions SELECT was called with LIMIT 3 bound param.
        fleet_selects = [
            (sql, params)
            for (sql, params) in cursor.executed
            if "FROM dispatcher.diagnoses d" in sql and "interval '6 hours'" in sql
        ]
        assert fleet_selects[0][1] == (3,)


# --------------------------------------------------------------------------
# AC#5 — new action handlers
# --------------------------------------------------------------------------


class TestBlockAndCommentHandler:
    def test_applies_blocked_removes_ready_posts_comment(self) -> None:
        d = _make_daemon()
        with (
            patch.object(d, "_gh_issue_comment") as mock_comment,
            patch.object(d, "_gh_issue_add_labels") as mock_add,
            patch.object(d, "_gh_issue_remove_labels") as mock_remove,
            patch.object(d, "_mark_agent_terminal") as mock_mark,
        ):
            d._consume_action_block_and_comment(
                agent_id="agent-1",
                issue_number=99001,
                reasoning="PAT scope missing — operator must update Secrets Manager.",
            )
        mock_comment.assert_called_once()
        body = mock_comment.call_args.args[1]
        assert "PAT scope missing" in body
        assert "3D block" in body
        mock_add.assert_called_once_with(99001, ["status/blocked"])
        mock_remove.assert_called_once_with(99001, ["agent/ready"])
        mock_mark.assert_called_once()

    def test_no_issue_number_still_marks_terminal(self) -> None:
        d = _make_daemon()
        with (
            patch.object(d, "_gh_issue_comment") as mock_comment,
            patch.object(d, "_mark_agent_terminal") as mock_mark,
        ):
            d._consume_action_block_and_comment(
                agent_id="agent-1",
                issue_number=None,
                reasoning="whatever",
            )
        mock_comment.assert_not_called()
        mock_mark.assert_called_once()


class TestFilePrerequisiteTaskHandler:
    def test_creates_new_issue_and_blocks_current(self) -> None:
        d = _make_daemon()
        with (
            patch.object(d, "_gh_issue_create", return_value=3050) as mock_create,
            patch.object(d, "_gh_issue_comment") as mock_comment,
            patch.object(d, "_gh_issue_append_body") as mock_append,
            patch.object(d, "_gh_issue_add_labels") as mock_add,
            patch.object(d, "_gh_issue_remove_labels") as mock_remove,
            patch.object(d, "_mark_agent_terminal") as mock_mark,
        ):
            d._consume_action_file_prerequisite_task(
                agent_id="agent-1",
                issue_number=99001,
                title="chore(infra): add workflow scope to dispatcher PAT",
                body="## Goal\nAdd scope.\n",
                block_labels=["priority/p1"],
                reasoning="Fleet-wide PAT scope blocker.",
            )
        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        assert kwargs["title"] == "chore(infra): add workflow scope to dispatcher PAT"
        assert kwargs["body"] == "## Goal\nAdd scope.\n"
        assert kwargs["labels"] == ["priority/p1"]
        mock_comment.assert_called_once()
        assert "#3050" in mock_comment.call_args.args[1]
        mock_append.assert_called_once_with(99001, "\n\nBlocked by #3050")
        mock_add.assert_called_once_with(99001, ["status/blocked"])
        mock_remove.assert_called_once_with(99001, ["agent/ready"])
        mock_mark.assert_called_once()

    def test_create_failure_falls_back_to_escalate(self) -> None:
        d = _make_daemon()
        with (
            patch.object(d, "_gh_issue_create", return_value=None),
            patch.object(d, "_consume_action_escalate") as mock_esc,
            patch.object(d, "_gh_issue_append_body") as mock_append,
        ):
            d._consume_action_file_prerequisite_task(
                agent_id="agent-1",
                issue_number=99001,
                title="x",
                body="y",
                block_labels=None,
                reasoning="root",
            )
        # Fall through to escalate; no body append.
        mock_esc.assert_called_once()
        mock_append.assert_not_called()


class TestBlockOnExistingTaskHandler:
    def test_validates_blocker_and_appends_body(self) -> None:
        d = _make_daemon()
        with (
            patch.object(
                d,
                "_gh_issue_is_open_with_detail",
                return_value=(True, {"attempts": 1, "reason": None, "stderr_tail": ""}),
            ),
            patch.object(d, "_gh_issue_comment") as mock_comment,
            patch.object(d, "_gh_issue_append_body") as mock_append,
            patch.object(d, "_gh_issue_add_labels") as mock_add,
            patch.object(d, "_gh_issue_remove_labels") as mock_remove,
            patch.object(d, "_mark_agent_terminal") as mock_mark,
        ):
            d._consume_action_block_on_existing_task(
                agent_id="agent-1",
                issue_number=99001,
                blocker_issue_number=3050,
                reasoning="Existing issue tracks this.",
            )
        mock_comment.assert_called_once()
        assert "#3050" in mock_comment.call_args.args[1]
        mock_append.assert_called_once_with(99001, "\n\nBlocked by #3050")
        mock_add.assert_called_once_with(99001, ["status/blocked"])
        mock_remove.assert_called_once_with(99001, ["agent/ready"])
        mock_mark.assert_called_once()

    def test_invalid_blocker_falls_back_to_escalate(self) -> None:
        d = _make_daemon()
        with (
            patch.object(
                d,
                "_gh_issue_is_open_with_detail",
                return_value=(
                    False,
                    {"attempts": 3, "reason": "timeout", "stderr_tail": ""},
                ),
            ),
            patch.object(d, "_consume_action_escalate") as mock_esc,
            patch.object(d, "_gh_issue_append_body") as mock_append,
        ):
            d._consume_action_block_on_existing_task(
                agent_id="agent-1",
                issue_number=99001,
                blocker_issue_number=9999,
                reasoning="missing blocker",
            )
        mock_esc.assert_called_once()
        mock_append.assert_not_called()

    def test_invalid_blocker_emits_validation_failed_warning(self, caplog: Any) -> None:
        """Issue #3053 — the silent fallback to escalate is now visible
        via a distinct WARNING event carrying the blocker number,
        current issue number, and the is-open probe's attempt count.
        """
        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon")
        with (
            patch.object(
                d,
                "_gh_issue_is_open_with_detail",
                return_value=(
                    False,
                    {"attempts": 3, "reason": "timeout", "stderr_tail": ""},
                ),
            ),
            patch.object(d, "_consume_action_escalate") as mock_esc,
        ):
            d._consume_action_block_on_existing_task(
                agent_id="agent-1",
                issue_number=99001,
                blocker_issue_number=9999,
                reasoning="blocker probe timed out",
            )
        mock_esc.assert_called_once()
        # The new WARNING event fires exactly once on the fallback path.
        validation_records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "block_on_existing_task_validation_failed"
        ]
        assert len(validation_records) == 1
        rec = validation_records[0]
        assert rec.levelno == logging.WARNING
        assert rec.issue_number == 99001
        assert rec.blocker_issue_number == 9999
        assert rec.attempts == 3
        assert rec.reason == "timeout"

    def test_agent_terminates_as_escalate_when_validation_fails(self) -> None:
        """Issue #3053 — the fallback path must still terminate the
        agent via ``_consume_action_escalate`` (which marks the agent
        ``diagnoser_escalate``). The new WARNING is additive; it does
        not change the control flow.
        """
        d = _make_daemon()
        with (
            patch.object(
                d,
                "_gh_issue_is_open_with_detail",
                return_value=(
                    False,
                    {"attempts": 3, "reason": "timeout", "stderr_tail": ""},
                ),
            ),
            patch.object(d, "_consume_action_escalate") as mock_esc,
            patch.object(d, "_gh_issue_comment") as mock_comment,
            patch.object(d, "_gh_issue_append_body") as mock_append,
            patch.object(d, "_gh_issue_add_labels") as mock_add,
            patch.object(d, "_gh_issue_remove_labels") as mock_remove,
        ):
            d._consume_action_block_on_existing_task(
                agent_id="agent-1",
                issue_number=99001,
                blocker_issue_number=9999,
                reasoning="blocker probe failed",
            )
        mock_esc.assert_called_once()
        # The "happy path" side effects must NOT fire on the fallback.
        mock_comment.assert_not_called()
        mock_append.assert_not_called()
        mock_add.assert_not_called()
        mock_remove.assert_not_called()

    def test_no_current_issue_falls_back_to_escalate(self) -> None:
        d = _make_daemon()
        with patch.object(d, "_consume_action_escalate") as mock_esc:
            d._consume_action_block_on_existing_task(
                agent_id="agent-1",
                issue_number=None,
                blocker_issue_number=3050,
                reasoning="no current issue",
            )
        mock_esc.assert_called_once()


# --------------------------------------------------------------------------
# AC#6/7 — hardened parse path: escalate on malformed / unknown / missing
# --------------------------------------------------------------------------


class TestHardenedParse:
    def test_missing_recommendation_escalates_not_retries(self) -> None:
        """Issue #3032 — parse failure falls back to escalate (NOT retry)."""
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        with (
            patch.object(d, "_read_recommendation", return_value=None),
            patch.object(d, "_mark_diagnosis_failed") as mock_marked_failed,
            patch.object(d, "_apply_mechanical_escalation") as mock_escalate,
            patch.object(d, "_create_retry_marker") as mock_retry,
        ):
            action = d._consume_diagnosis(diagnosis_id=1, candidate=candidate)
        assert action == "escalate_fallback"
        mock_marked_failed.assert_called_once()
        mock_escalate.assert_called_once()
        mock_retry.assert_not_called()  # must NOT silently retry

    def test_missing_action_field_escalates(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        with (
            patch.object(d, "_read_recommendation", return_value={"reasoning": "..."}),
            patch.object(d, "_apply_mechanical_escalation") as mock_escalate,
            patch.object(d, "_create_retry_marker") as mock_retry,
        ):
            action = d._consume_diagnosis(diagnosis_id=2, candidate=candidate)
        assert action == "escalate_fallback"
        mock_escalate.assert_called_once()
        mock_retry.assert_not_called()

    def test_unknown_action_persists_row_and_escalates(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        rec = {"action": "split_task", "reasoning": "LLM got creative"}
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_persist_unrecognized_action") as mock_persist,
            patch.object(d, "_apply_mechanical_escalation") as mock_escalate,
            patch.object(d, "_create_retry_marker") as mock_retry,
        ):
            action = d._consume_diagnosis(diagnosis_id=3, candidate=candidate)
        assert action == "escalate_fallback"
        mock_persist.assert_called_once()
        kwargs = mock_persist.call_args.kwargs
        assert kwargs["diagnosis_id"] == 3
        assert kwargs["action_name"] == "split_task"
        assert kwargs["payload"] == rec
        mock_escalate.assert_called_once()
        mock_retry.assert_not_called()

    def test_retry_with_hint_missing_hint_escalates(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        rec = {"action": "retry_with_hint", "reasoning": "x"}  # hint missing
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_apply_mechanical_escalation") as mock_escalate,
            patch.object(d, "_create_retry_marker") as mock_retry,
        ):
            action = d._consume_diagnosis(diagnosis_id=4, candidate=candidate)
        assert action == "escalate_fallback"
        mock_escalate.assert_called_once()
        mock_retry.assert_not_called()

    def test_file_prerequisite_task_missing_body_escalates(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        rec = {
            "action": "file_prerequisite_task",
            "title": "chore: x",
            "reasoning": "y",
        }
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_apply_mechanical_escalation") as mock_escalate,
        ):
            action = d._consume_diagnosis(diagnosis_id=5, candidate=candidate)
        assert action == "escalate_fallback"
        mock_escalate.assert_called_once()

    def test_block_on_existing_task_missing_blocker_escalates(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        rec = {"action": "block_on_existing_task", "reasoning": "y"}
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_apply_mechanical_escalation") as mock_escalate,
        ):
            action = d._consume_diagnosis(diagnosis_id=6, candidate=candidate)
        assert action == "escalate_fallback"
        mock_escalate.assert_called_once()


# --------------------------------------------------------------------------
# AC#6 — raw LLM output logged on parse failure
# --------------------------------------------------------------------------


class TestRawOutputLogging:
    def test_diagnoser_parse_failed_event_logged_on_unknown(self, caplog: Any) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        rec = {"action": "weird_thing", "reasoning": "?"}
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_persist_unrecognized_action"),
            patch.object(d, "_apply_mechanical_escalation"),
            caplog.at_level(logging.WARNING, logger="test.daemon"),
        ):
            d._consume_diagnosis(diagnosis_id=99, candidate=candidate)
        # Assert the daemon.diagnoser_parse_failed event fired with
        # a non-empty raw_output string.
        matching = [r for r in caplog.records if r.name == "test.daemon"]
        parse_failed_records = [
            r for r in matching if getattr(r, "event", "") == "diagnoser_parse_failed"
        ]
        assert parse_failed_records, "expected diagnoser_parse_failed event"
        r = parse_failed_records[0]
        assert getattr(r, "reason", "") == "unrecognized_action"
        raw_output = getattr(r, "raw_output", None)
        assert raw_output is not None
        assert "weird_thing" in raw_output

    def test_none_recommendation_logs_raw_output_null(self, caplog: Any) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        with (
            patch.object(d, "_read_recommendation", return_value=None),
            patch.object(d, "_apply_mechanical_escalation"),
            caplog.at_level(logging.WARNING, logger="test.daemon"),
        ):
            d._consume_diagnosis(diagnosis_id=100, candidate=candidate)
        matching = [r for r in caplog.records if r.name == "test.daemon"]
        parse_failed_records = [
            r for r in matching if getattr(r, "event", "") == "diagnoser_parse_failed"
        ]
        assert parse_failed_records
        r = parse_failed_records[0]
        assert getattr(r, "raw_output", "sentinel") is None


# --------------------------------------------------------------------------
# AC#6 — _persist_unrecognized_action writes the expected row
# --------------------------------------------------------------------------


class TestPersistUnrecognizedAction:
    def test_insert_shape(self) -> None:
        cursor = _FakeCursor()
        d = _make_daemon(cursor)
        payload = {"action": "split_task", "reasoning": "r"}
        d._persist_unrecognized_action(
            diagnosis_id=7, action_name="split_task", payload=payload
        )
        assert len(cursor.executed) == 1
        sql, params = cursor.executed[0]
        assert "INSERT INTO dispatcher.unrecognized_diagnoser_actions" in sql
        assert params[0] == 7
        assert params[1] == "split_task"
        # payload serialized to JSON string.
        assert "split_task" in params[2]

    def test_db_error_does_not_propagate(self) -> None:
        """A bad INSERT must not block the escalate fallback path."""
        cursor = _FakeCursor()

        def _boom(_sql: str, _params: Any = None) -> None:
            raise RuntimeError("boom")

        cursor.execute = _boom  # type: ignore[assignment]
        d = _make_daemon(cursor)
        # Should not raise.
        d._persist_unrecognized_action(
            diagnosis_id=8, action_name="x", payload={"a": 1}
        )


# --------------------------------------------------------------------------
# AC#5 — _consume_diagnosis dispatches to new handlers
# --------------------------------------------------------------------------


class TestConsumeDiagnosisDispatchesNewActions:
    def test_block_and_comment_dispatch(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        rec = {"action": "block_and_comment", "reasoning": "need operator"}
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_consume_action_block_and_comment") as mock_handler,
        ):
            action = d._consume_diagnosis(diagnosis_id=20, candidate=candidate)
        assert action == "block_and_comment"
        mock_handler.assert_called_once_with(
            agent_id="agent-1", issue_number=99001, reasoning="need operator"
        )

    def test_file_prerequisite_task_dispatch(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        rec = {
            "action": "file_prerequisite_task",
            "title": "chore: x",
            "body": "## Goal\nx",
            "reasoning": "root cause",
            "block_labels": ["priority/p1"],
        }
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_consume_action_file_prerequisite_task") as mock_handler,
        ):
            action = d._consume_diagnosis(diagnosis_id=21, candidate=candidate)
        assert action == "file_prerequisite_task"
        mock_handler.assert_called_once()
        kwargs = mock_handler.call_args.kwargs
        assert kwargs["title"] == "chore: x"
        assert kwargs["body"] == "## Goal\nx"
        assert kwargs["block_labels"] == ["priority/p1"]

    def test_block_on_existing_task_dispatch(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        rec = {
            "action": "block_on_existing_task",
            "blocker_issue_number": 3050,
            "reasoning": "exists",
        }
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_consume_action_block_on_existing_task") as mock_handler,
        ):
            action = d._consume_diagnosis(diagnosis_id=22, candidate=candidate)
        assert action == "block_on_existing_task"
        mock_handler.assert_called_once()
        kwargs = mock_handler.call_args.kwargs
        assert kwargs["blocker_issue_number"] == 3050


# --------------------------------------------------------------------------
# Regression — existing five actions still dispatch correctly
# --------------------------------------------------------------------------


class TestRegressionExistingActions:
    def test_retry_still_dispatches(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_SUBPROCESS_CRASH,
        }
        rec = {"action": "retry", "reasoning": "transient"}
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_consume_action_retry") as mock_handler,
        ):
            action = d._consume_diagnosis(diagnosis_id=30, candidate=candidate)
        assert action == "retry"
        mock_handler.assert_called_once()

    def test_reissue_still_dispatches(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_RALPH_AC_INFEASIBLE,
        }
        rec = {
            "action": "reissue",
            "reasoning": "AC outdated",
            "new_scope": "## Goal\nRewritten body\n",
        }
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_consume_action_reissue") as mock_handler,
        ):
            action = d._consume_diagnosis(diagnosis_id=31, candidate=candidate)
        assert action == "reissue"
        mock_handler.assert_called_once()

    def test_escalate_still_dispatches(self) -> None:
        d = _make_daemon()
        candidate = {
            "agent_id": "agent-1",
            "issue_number": 99001,
            "category": FAILURE_CATEGORY_SUMMARY_AC_INFEASIBLE,
        }
        rec = {"action": "escalate", "reasoning": "needs human"}
        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, "_consume_action_escalate") as mock_handler,
            patch.object(d, "_mark_diagnosis_completed"),
        ):
            action = d._consume_diagnosis(diagnosis_id=32, candidate=candidate)
        assert action == "escalate"
        mock_handler.assert_called_once()


# --------------------------------------------------------------------------
# Issue #3422 — lifecycle: _mark_diagnosis_completed + directive_applied
# --------------------------------------------------------------------------


class TestConsumeDiagnosisLifecycle:
    """After _consume_diagnosis succeeds, the daemon must:
    (a) call _mark_diagnosis_completed with the diagnosis_id,
    (b) emit a daemon.directive_applied log event with the action field.

    For each of the four primary action shapes (close, escalate,
    block_and_comment, file_prerequisite_task) plus one negative path.
    """

    _CANDIDATE: dict[str, Any] = {
        "agent_id": "agent-lc-1",
        "issue_number": 99002,
        "category": FAILURE_CATEGORY_PUSH_FAILED,
    }

    def _run_with_action(
        self,
        action: str,
        rec: dict[str, Any],
        caplog: Any,
    ) -> tuple[str, list[int]]:
        """Helper: run _consume_diagnosis with stubbed handler and capture calls."""
        import logging

        d = _make_daemon()
        caplog.set_level(logging.INFO, logger="test.daemon")
        mark_called: list[int] = []

        handler_map = {
            "close": "_consume_action_close",
            "escalate": "_consume_action_escalate",
            "block_and_comment": "_consume_action_block_and_comment",
            "file_prerequisite_task": "_consume_action_file_prerequisite_task",
            "retry": "_consume_action_retry",
        }
        handler_attr = handler_map.get(action, f"_consume_action_{action}")

        with (
            patch.object(d, "_read_recommendation", return_value=rec),
            patch.object(d, handler_attr),
            patch.object(
                d,
                "_mark_diagnosis_completed",
                side_effect=lambda did: mark_called.append(did),
            ),
        ):
            result = d._consume_diagnosis(diagnosis_id=55, candidate=self._CANDIDATE)

        return result, mark_called

    def test_close_marks_completed_and_emits_event(self, caplog: Any) -> None:
        rec = {"action": "close", "reasoning": "issue is invalid"}
        result, mark_called = self._run_with_action("close", rec, caplog)
        assert result == "close"
        assert mark_called == [55], (
            "_mark_diagnosis_completed not called with diagnosis_id=55"
        )
        events = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "directive_applied"
        ]
        assert events, "daemon.directive_applied event not emitted"
        assert getattr(events[0], "action", None) == "close"

    def test_escalate_marks_completed_and_emits_event(self, caplog: Any) -> None:
        rec = {"action": "escalate", "reasoning": "needs human"}
        result, mark_called = self._run_with_action("escalate", rec, caplog)
        assert result == "escalate"
        assert mark_called == [55]
        events = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "directive_applied"
        ]
        assert events
        assert getattr(events[0], "action", None) == "escalate"

    def test_block_and_comment_marks_completed_and_emits_event(
        self, caplog: Any
    ) -> None:
        rec = {"action": "block_and_comment", "reasoning": "operator must act"}
        result, mark_called = self._run_with_action("block_and_comment", rec, caplog)
        assert result == "block_and_comment"
        assert mark_called == [55]
        events = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "directive_applied"
        ]
        assert events
        assert getattr(events[0], "action", None) == "block_and_comment"

    def test_file_prerequisite_task_marks_completed_and_emits_event(
        self, caplog: Any
    ) -> None:
        rec = {
            "action": "file_prerequisite_task",
            "title": "chore: x",
            "body": "## Goal\nx",
            "reasoning": "root cause",
        }
        result, mark_called = self._run_with_action(
            "file_prerequisite_task", rec, caplog
        )
        assert result == "file_prerequisite_task"
        assert mark_called == [55]
        events = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "directive_applied"
        ]
        assert events
        assert getattr(events[0], "action", None) == "file_prerequisite_task"

    def test_malformed_recommendation_fires_failed_events(self, caplog: Any) -> None:
        """Negative path: malformed recommendation → _mark_diagnosis_failed +
        daemon.directive_apply_failed fire; _mark_diagnosis_completed does NOT."""
        import logging

        d = _make_daemon()
        caplog.set_level(logging.WARNING, logger="test.daemon")
        candidate = {
            "agent_id": "agent-lc-1",
            "issue_number": 99002,
            "category": FAILURE_CATEGORY_PUSH_FAILED,
        }
        mark_failed_calls: list[Any] = []
        mark_completed_calls: list[Any] = []

        with (
            patch.object(d, "_read_recommendation", return_value=None),
            patch.object(d, "_apply_mechanical_escalation"),
            patch.object(
                d,
                "_mark_diagnosis_failed",
                side_effect=lambda did, **kw: mark_failed_calls.append(did),
            ),
            patch.object(
                d,
                "_mark_diagnosis_completed",
                side_effect=lambda did: mark_completed_calls.append(did),
            ),
        ):
            result = d._consume_diagnosis(diagnosis_id=56, candidate=candidate)

        assert result == "escalate_fallback"
        assert mark_failed_calls == [56], "_mark_diagnosis_failed must fire on fallback"
        assert mark_completed_calls == [], (
            "_mark_diagnosis_completed must NOT fire on fallback"
        )

        apply_failed_events = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "directive_apply_failed"
        ]
        assert apply_failed_events, "daemon.directive_apply_failed event not emitted"
        event_rec = apply_failed_events[0]
        assert getattr(event_rec, "diagnosis_id", None) == 56
        assert getattr(event_rec, "action", "sentinel") is None
