"""Unit tests for :mod:`scripts.dispatcher.phase_transitions` (#3086).

This module is the pure-function extract of the daemon's
phase-transition state machine. The tests exercise every branch of
every transition function so a later refactor of the daemon to use
this module (see #3086 Stage 2 followup) can drop into the same
behavior without behavior drift.

Structure: one test class per transition function plus one test
class for the convenience helpers. Every ``TransitionAction`` branch
is exercised by at least one test case.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import phase_transitions as pt  # noqa: E402


# --------------------------------------------------------------------------
# transition_from_plan
# --------------------------------------------------------------------------


class TestTransitionFromPlan:
    """Plan-phase transitions: happy path + BLOCKED terminal."""

    def test_happy_path_advances_to_ralph(self) -> None:
        result = pt.transition_from_plan({"plan_doc": "..."})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_RALPH
        assert result.terminal_status is None

    def test_missing_verdict_still_advances(self) -> None:
        # Plan's primary deliverable is the plan doc; absence of a
        # verdict field is treated as "plan done, proceed".
        result = pt.transition_from_plan({})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_RALPH

    def test_none_output_advances(self) -> None:
        # Defensive — the daemon should always produce output, but a
        # None input must not crash.
        result = pt.transition_from_plan(None)
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_RALPH

    def test_blocked_verdict_marks_plan_blocked(self) -> None:
        result = pt.transition_from_plan(
            {"verdict": "BLOCKED", "block_reason": "ambiguous AC #3"}
        )
        assert result.action == pt.TransitionAction.ADVANCE_WITH_STATUS
        assert result.next_phase == pt.PHASE_PLAN_BLOCKED
        assert result.terminal_status == pt.AgentStatus.PLAN_BLOCKED.value
        assert result.failure_hint == pt.FAILURE_HINT_PLAN_BLOCKED
        assert result.context.get("block_reason") == "ambiguous AC #3"

    def test_blocked_verdict_lowercase_still_matches(self) -> None:
        # Verdict matching is case-insensitive via str.upper().
        result = pt.transition_from_plan({"verdict": "blocked"})
        assert result.action == pt.TransitionAction.ADVANCE_WITH_STATUS
        assert result.next_phase == pt.PHASE_PLAN_BLOCKED


# --------------------------------------------------------------------------
# transition_from_ralph
# --------------------------------------------------------------------------


class TestTransitionFromRalph:
    """Ralph-phase transitions: SHIP, AC_INFEASIBLE, non-SHIP terminal."""

    def test_ship_advances_to_summary(self) -> None:
        result = pt.transition_from_ralph({"verdict": "SHIP", "iterations_used": 2})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_SUMMARY

    def test_ac_infeasible_routes_to_diagnoser(self) -> None:
        result = pt.transition_from_ralph(
            {
                "verdict": "AC_INFEASIBLE",
                "infeasible_acs": [{"index": 3, "evidence": "no API exists"}],
            }
        )
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_RALPH_AC_INFEASIBLE
        assert result.next_phase is None
        # Context carries the infeasible_acs so the caller can write a
        # faithful failure row.
        assert result.context["infeasible_acs"] == [
            {"index": 3, "evidence": "no API exists"}
        ]

    def test_revise_exhausted_routes_to_diagnoser(self) -> None:
        # Non-SHIP, non-AC_INFEASIBLE = RALPH_NOT_SHIP per #3054.
        result = pt.transition_from_ralph(
            {
                "verdict": "REVISE",
                "block_reason": "3 iterations of REVISE",
                "iterations_used": 3,
            }
        )
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_RALPH_NOT_SHIP
        assert result.context["verdict"] == "REVISE"
        assert result.context["block_reason"] == "3 iterations of REVISE"
        assert result.context["iterations_used"] == 3

    def test_stuck_verdict_routes_to_diagnoser_as_not_ship(self) -> None:
        result = pt.transition_from_ralph({"verdict": "STUCK"})
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_RALPH_NOT_SHIP

    def test_missing_verdict_routes_to_diagnoser(self) -> None:
        # A ralph output with no verdict field at all is treated as
        # non-SHIP (ralph promised to always emit one) — diagnoser
        # picks up the broken-skill failure.
        result = pt.transition_from_ralph({})
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_RALPH_NOT_SHIP
        assert result.context["verdict"] == ""

    def test_none_output_routes_to_diagnoser(self) -> None:
        result = pt.transition_from_ralph(None)
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_RALPH_NOT_SHIP


# --------------------------------------------------------------------------
# transition_from_summary
# --------------------------------------------------------------------------


class TestTransitionFromSummary:
    """Summary-phase transitions: happy path + AC_INFEASIBLE."""

    def test_happy_path_advances_to_push_and_pr(self) -> None:
        result = pt.transition_from_summary(
            {"commit_message": "feat(x): y", "pr_body": "..."}
        )
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_PUSH_AND_PR

    def test_ac_infeasible_routes_to_diagnoser(self) -> None:
        result = pt.transition_from_summary(
            {
                "verdict": "AC_INFEASIBLE",
                "infeasible_acs": [{"index": 2, "evidence": "wrong shape"}],
            }
        )
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_SUMMARY_AC_INFEASIBLE
        assert result.context["infeasible_acs"] == [
            {"index": 2, "evidence": "wrong shape"}
        ]

    def test_missing_verdict_advances(self) -> None:
        # Summary does not gate on verdict when absent — the primary
        # deliverable is the commit_message + pr_body.
        result = pt.transition_from_summary({})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_PUSH_AND_PR


# --------------------------------------------------------------------------
# transition_from_push_and_pr
# --------------------------------------------------------------------------


class TestTransitionFromPushAndPr:
    """push_and_pr transitions: happy path + no-op SHIP terminal (#3039)."""

    def test_happy_path_advances_to_awaiting_ci(self) -> None:
        result = pt.transition_from_push_and_pr({"pr_number": 1234})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_no_op_ship_marks_no_op_terminal(self) -> None:
        result = pt.transition_from_push_and_pr({"no_op": True})
        assert result.action == pt.TransitionAction.ADVANCE_WITH_STATUS
        assert result.next_phase == pt.PHASE_NO_OP
        assert result.terminal_status == pt.AgentStatus.SUCCEEDED.value

    def test_no_op_false_advances_normally(self) -> None:
        result = pt.transition_from_push_and_pr({"no_op": False})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_missing_no_op_advances_normally(self) -> None:
        result = pt.transition_from_push_and_pr({})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_AWAITING_CI


# --------------------------------------------------------------------------
# transition_from_awaiting_ci
# --------------------------------------------------------------------------


class TestTransitionFromAwaitingCi:
    """CI-rollup transitions: green / red / pending branches."""

    def _make_status(
        self,
        rollup: list[dict],
        *,
        mergeable: str = "MERGEABLE",
        merge_state: str = "CLEAN",
    ) -> dict:
        return {
            "statusCheckRollup": rollup,
            "mergeable": mergeable,
            "mergeStateStatus": merge_state,
        }

    def test_all_green_advances_to_merge(self) -> None:
        status = self._make_status(
            [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
                {"status": "COMPLETED", "conclusion": "SKIPPED", "name": "deploy"},
                {"status": "COMPLETED", "conclusion": "NEUTRAL", "name": "codecov"},
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_MERGE

    def test_any_failure_advances_to_fix_ci(self) -> None:
        status = self._make_status(
            [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": "test"},
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_FIX_CI

    def test_cancelled_counts_as_red(self) -> None:
        status = self._make_status(
            [
                {"status": "COMPLETED", "conclusion": "CANCELLED", "name": "test"},
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_FIX_CI

    def test_timed_out_counts_as_red(self) -> None:
        status = self._make_status(
            [
                {"status": "COMPLETED", "conclusion": "TIMED_OUT", "name": "test"},
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_FIX_CI

    def test_action_required_counts_as_red(self) -> None:
        status = self._make_status(
            [
                {"status": "COMPLETED", "conclusion": "ACTION_REQUIRED", "name": "x"},
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_FIX_CI

    def test_startup_failure_counts_as_red(self) -> None:
        status = self._make_status(
            [
                {"status": "COMPLETED", "conclusion": "STARTUP_FAILURE", "name": "x"},
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_FIX_CI

    def test_in_progress_stays_pending(self) -> None:
        status = self._make_status(
            [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
                {"status": "IN_PROGRESS", "conclusion": None, "name": "test"},
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.action == pt.TransitionAction.ADVANCE
        # Pending = stay put.
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_queued_stays_pending(self) -> None:
        status = self._make_status(
            [
                {"status": "QUEUED", "conclusion": None, "name": "test"},
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_mergeable_not_mergeable_stays_pending(self) -> None:
        status = self._make_status(
            [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
            ],
            mergeable="CONFLICTING",
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_merge_state_not_clean_stays_pending(self) -> None:
        status = self._make_status(
            [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
            ],
            merge_state="DIRTY",
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_none_status_pending(self) -> None:
        result = pt.transition_from_awaiting_ci(None)
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_empty_rollup_green_when_mergeable(self) -> None:
        # Zero checks + MERGEABLE+CLEAN is "nothing red, nothing
        # pending" — green. Defensive since real PRs always have CI.
        status = self._make_status([])
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_MERGE

    def test_unrecognized_conclusion_treated_as_red(self) -> None:
        # COMPLETED with a bogus conclusion is red, not silently
        # green — defense against future GitHub API additions.
        status = self._make_status(
            [
                {"status": "COMPLETED", "conclusion": "WAT", "name": "test"},
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_FIX_CI

    def test_non_list_rollup_treated_as_pending(self) -> None:
        # Defensive — if gh returns a non-list statusCheckRollup
        # (e.g. an error envelope), we stay pending rather than
        # crashing or silently advancing.
        status = {
            "statusCheckRollup": "error",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_non_dict_check_entry_skipped(self) -> None:
        # Defensive — a non-dict entry in the rollup list is
        # skipped; a valid SUCCESS entry after it still counts.
        status = self._make_status(
            [
                "not a dict",
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_MERGE

    # ----- #3200 StatusContext coverage ------------------------------------

    def test_statuscontext_success_counts_as_green(self) -> None:
        # Regression for #3200 — the live-reproducible bug: Vercel-
        # style StatusContext entries have no ``.status``/``.conclusion``,
        # only ``.state``. Pre-fix the classifier fell through to
        # "pending" so every PR with a Vercel status was stuck.
        status = self._make_status(
            [
                {
                    "__typename": "CheckRun",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "name": "lint",
                },
                {
                    "__typename": "StatusContext",
                    "context": "Vercel",
                    "state": "SUCCESS",
                },
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_MERGE

    def test_statuscontext_failure_counts_as_red(self) -> None:
        status = self._make_status(
            [
                {
                    "__typename": "StatusContext",
                    "context": "Vercel",
                    "state": "FAILURE",
                },
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_FIX_CI

    def test_statuscontext_error_counts_as_red(self) -> None:
        status = self._make_status(
            [
                {
                    "__typename": "StatusContext",
                    "context": "build",
                    "state": "ERROR",
                },
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_FIX_CI

    def test_statuscontext_pending_stays_pending(self) -> None:
        # PENDING / EXPECTED state — StatusContext entry hasn't
        # completed; must stay in awaiting_ci rather than returning
        # green.
        status = self._make_status(
            [
                {
                    "__typename": "CheckRun",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "name": "lint",
                },
                {
                    "__typename": "StatusContext",
                    "context": "Vercel",
                    "state": "PENDING",
                },
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_statuscontext_expected_stays_pending(self) -> None:
        status = self._make_status(
            [
                {
                    "__typename": "StatusContext",
                    "context": "Vercel",
                    "state": "EXPECTED",
                },
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_mixed_checkrun_and_statuscontext_all_green(self) -> None:
        # Real PR #3198 shape — 3 CheckRuns + 1 Vercel StatusContext,
        # all success, MERGEABLE + CLEAN.
        status = self._make_status(
            [
                {
                    "__typename": "CheckRun",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "name": "lint",
                },
                {
                    "__typename": "CheckRun",
                    "status": "COMPLETED",
                    "conclusion": "SKIPPED",
                    "name": "build",
                },
                {
                    "__typename": "CheckRun",
                    "status": "COMPLETED",
                    "conclusion": "NEUTRAL",
                    "name": "codecov",
                },
                {
                    "__typename": "StatusContext",
                    "context": "Vercel",
                    "state": "SUCCESS",
                },
            ]
        )
        result = pt.transition_from_awaiting_ci(status)
        assert result.next_phase == pt.PHASE_MERGE


# --------------------------------------------------------------------------
# transition_from_fix_ci
# --------------------------------------------------------------------------


class TestTransitionFromFixCi:
    """Fix-CI transitions: PATCHED / FLAKY loop, BLOCKED terminal."""

    def test_patched_returns_to_awaiting_ci(self) -> None:
        result = pt.transition_from_fix_ci({"verdict": "PATCHED"})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_AWAITING_CI
        assert result.context["patch_applied"] is True

    def test_flaky_stays_in_awaiting_ci_without_patch(self) -> None:
        result = pt.transition_from_fix_ci({"verdict": "FLAKY"})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_AWAITING_CI
        assert result.context["patch_applied"] is False

    def test_blocked_routes_to_diagnoser(self) -> None:
        result = pt.transition_from_fix_ci(
            {"verdict": "BLOCKED", "block_reason": "depends on upstream bug"}
        )
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_FIX_CI_BLOCKED
        assert result.context["block_reason"] == "depends on upstream bug"

    def test_unrecognized_verdict_routes_to_diagnoser(self) -> None:
        # Per daemon.py lines 11258-11259: "verdict == 'BLOCKED' or
        # unrecognized" — both fall through to the same failure path.
        result = pt.transition_from_fix_ci({"verdict": "HUH"})
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_FIX_CI_BLOCKED

    def test_missing_verdict_routes_to_diagnoser(self) -> None:
        result = pt.transition_from_fix_ci({})
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_FIX_CI_BLOCKED


# --------------------------------------------------------------------------
# transition_from_fix_conflict (#3225)
# --------------------------------------------------------------------------


class TestTransitionFromFixConflict:
    """Fix-conflict transitions (#3225): RESOLVED re-enters push_and_pr, UNRESOLVABLE routes to diagnoser."""

    def test_resolved_advances_to_push_and_pr(self) -> None:
        result = pt.transition_from_fix_conflict(
            {
                "verdict": "resolved",
                "resolution_notes": "reconciled 2 hunks against main's refactor",
                "resolved_files": [
                    {"path": "packages/web/app/a.tsx", "content": "..."},
                    {"path": "packages/web/app/b.tsx", "content": "..."},
                ],
            }
        )
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_PUSH_AND_PR
        assert result.context["resolved_files_count"] == 2
        assert (
            result.context["resolution_notes"]
            == "reconciled 2 hunks against main's refactor"
        )

    def test_resolved_uppercase_still_matches(self) -> None:
        # Verdict matching is case-insensitive via str.upper() — skill
        # emits lowercase, daemon constants are upper.
        result = pt.transition_from_fix_conflict({"verdict": "RESOLVED"})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_PUSH_AND_PR

    def test_unresolvable_routes_to_diagnoser(self) -> None:
        result = pt.transition_from_fix_conflict(
            {
                "verdict": "unresolvable",
                "resolution_notes": "function X was rewritten on main",
                "conflict_files": ["packages/api/src/x.py"],
            }
        )
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_CONFLICT_UNRESOLVABLE
        assert result.context["verdict"] == "UNRESOLVABLE"
        assert result.context["resolution_notes"] == "function X was rewritten on main"
        assert result.context["conflict_files"] == ["packages/api/src/x.py"]
        assert result.context["budget_exhausted"] is False

    def test_budget_exhausted_routes_to_diagnoser(self) -> None:
        # Entrypoint emits this shape when merge_conflict_attempts >=
        # FIX_CONFLICT_MAX_ATTEMPTS — no claude was invoked. The
        # transition still routes to diagnoser; the budget_exhausted
        # flag propagates via context so the diagnoser can distinguish
        # "we never tried" from "we tried and failed semantically".
        result = pt.transition_from_fix_conflict(
            {
                "verdict": "unresolvable",
                "resolution_notes": "budget exhausted after 2 attempts",
                "budget_exhausted": True,
            }
        )
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_CONFLICT_UNRESOLVABLE
        assert result.context["budget_exhausted"] is True

    def test_missing_verdict_routes_to_diagnoser(self) -> None:
        result = pt.transition_from_fix_conflict({})
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_CONFLICT_UNRESOLVABLE
        assert result.context["verdict"] == ""

    def test_none_output_routes_to_diagnoser(self) -> None:
        result = pt.transition_from_fix_conflict(None)
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_CONFLICT_UNRESOLVABLE


# --------------------------------------------------------------------------
# transition_from_push_and_pr — rebase_failed branch (#3225)
# --------------------------------------------------------------------------


class TestTransitionFromPushAndPrRebaseConflict:
    """#3225: push_and_pr emits rebase_failed → advance to fix_conflict."""

    def test_rebase_failed_advances_to_fix_conflict(self) -> None:
        result = pt.transition_from_push_and_pr(
            {
                "rebase_failed": True,
                "conflict_files": [
                    "packages/web/app/(main)/admin/dispatcher/a.tsx",
                    "packages/web/app/(main)/admin/dispatcher/b.tsx",
                ],
            }
        )
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_FIX_CONFLICT
        assert result.context["conflict_files"] == [
            "packages/web/app/(main)/admin/dispatcher/a.tsx",
            "packages/web/app/(main)/admin/dispatcher/b.tsx",
        ]
        assert result.context["source_phase"] == "push_and_pr"

    def test_rebase_failed_takes_precedence_over_no_op(self) -> None:
        # Defensive — if the entrypoint somehow emits both (shouldn't
        # happen, but paranoia is cheap here), the no-op SHIP terminal
        # wins because it signals "ralph had no changes" which is
        # incompatible with "rebase conflicted on changes".
        result = pt.transition_from_push_and_pr({"rebase_failed": True, "no_op": True})
        # no_op wins (ordered check).
        assert result.action == pt.TransitionAction.ADVANCE_WITH_STATUS
        assert result.next_phase == pt.PHASE_NO_OP

    def test_missing_rebase_failed_advances_normally(self) -> None:
        result = pt.transition_from_push_and_pr({"pr_number": 4242})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_AWAITING_CI


# --------------------------------------------------------------------------
# transition_from_ralph — rebase_failed branch (#3225 secondary mitigation)
# --------------------------------------------------------------------------


class TestTransitionFromRalphBaselineRebaseConflict:
    """#3225: start-of-ralph baseline rebase failure → fix_conflict."""

    def test_rebase_failed_short_circuits_verdict_check(self) -> None:
        # The entrypoint emits rebase_failed BEFORE any claude
        # iterations ran, so there is no verdict field yet. Transition
        # must still advance to fix_conflict rather than falling into
        # the RALPH_NOT_SHIP diagnoser branch.
        result = pt.transition_from_ralph(
            {"rebase_failed": True, "conflict_files": ["x.py", "y.py"]}
        )
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_FIX_CONFLICT
        assert result.context["conflict_files"] == ["x.py", "y.py"]
        assert result.context["source_phase"] == "ralph"

    def test_rebase_failed_beats_verdict(self) -> None:
        # Even if a verdict is somehow present alongside rebase_failed,
        # the rebase-failure takes precedence — the claude iterations
        # never actually executed, so any verdict would be stale.
        result = pt.transition_from_ralph({"rebase_failed": True, "verdict": "SHIP"})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_FIX_CONFLICT

    def test_no_rebase_failed_advances_via_verdict(self) -> None:
        # Sanity check: pre-existing SHIP behaviour is preserved when
        # rebase_failed is absent.
        result = pt.transition_from_ralph({"verdict": "SHIP"})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_SUMMARY


# --------------------------------------------------------------------------
# transition_from_merge
# --------------------------------------------------------------------------


class TestTransitionFromMerge:
    """Merge transitions: success → awaiting_deploy w/ status flip."""

    def test_success_advances_to_awaiting_deploy_with_status(self) -> None:
        result = pt.transition_from_merge(merge_succeeded=True)
        assert result.action == pt.TransitionAction.ADVANCE_WITH_STATUS
        assert result.next_phase == pt.PHASE_AWAITING_DEPLOY
        assert result.terminal_status == pt.AgentStatus.SUCCEEDED.value
        assert result.context["is_self_deploy"] is False

    def test_self_deploy_context_propagates(self) -> None:
        result = pt.transition_from_merge(merge_succeeded=True, is_self_deploy=True)
        assert result.action == pt.TransitionAction.ADVANCE_WITH_STATUS
        assert result.context["is_self_deploy"] is True

    def test_merge_failure_returns_unrecognized(self) -> None:
        # Merge failures are handled by the daemon's rebase-retry loop,
        # not via phase transition. Return UNRECOGNIZED so the caller
        # cannot accidentally silently advance.
        result = pt.transition_from_merge(merge_succeeded=False)
        assert result.action == pt.TransitionAction.UNRECOGNIZED
        assert result.next_phase is None


# --------------------------------------------------------------------------
# transition_from_awaiting_deploy
# --------------------------------------------------------------------------


class TestTransitionFromAwaitingDeploy:
    """Deploy-watch transitions: success → verify (or skip), failure → diagnoser."""

    def test_success_advances_to_verify(self) -> None:
        result = pt.transition_from_awaiting_deploy(deploy_succeeded=True)
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_VERIFY

    def test_self_deploy_skips_verify(self) -> None:
        result = pt.transition_from_awaiting_deploy(
            deploy_succeeded=True, is_self_deploy=True
        )
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_DONE
        assert result.context.get("verify_skip_reason") == "self_deploy"

    def test_failure_routes_to_diagnoser(self) -> None:
        result = pt.transition_from_awaiting_deploy(deploy_succeeded=False)
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == "deploy_failed"


# --------------------------------------------------------------------------
# transition_from_verify
# --------------------------------------------------------------------------


class TestTransitionFromVerify:
    """Verify transitions: VERIFIED/SKIPPED → done, FAILED → diagnoser."""

    def test_verified_advances_to_done(self) -> None:
        result = pt.transition_from_verify({"verdict": "VERIFIED"})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_DONE

    def test_skipped_advances_to_done(self) -> None:
        result = pt.transition_from_verify({"verdict": "SKIPPED"})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_DONE

    def test_failed_routes_to_diagnoser(self) -> None:
        result = pt.transition_from_verify(
            {
                "verdict": "FAILED",
                "failure_reason": "endpoint returns 500",
                "evidence_md": "curl -X POST ...",
            }
        )
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_VERIFY_FAILED_POST_MERGE
        assert result.context["failure_reason"] == "endpoint returns 500"
        assert result.context["evidence_md"] == "curl -X POST ..."

    def test_missing_verdict_advances_to_done(self) -> None:
        # Mirrors daemon.py behavior — verify's default is "move on"
        # rather than "treat absence as failure".
        result = pt.transition_from_verify({})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_DONE


# --------------------------------------------------------------------------
# transition_from_retro
# --------------------------------------------------------------------------


class TestTransitionFromRetro:
    """Retro transitions: both success + failure preserve agent success."""

    def test_success_advances_to_retro_done(self) -> None:
        result = pt.transition_from_retro(retro_succeeded=True)
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_RETRO_DONE

    def test_failure_advances_to_retro_failed(self) -> None:
        # Retro failure does NOT flip agent status; it's post-success
        # bookkeeping. Cleanup runs from retro_failed too.
        result = pt.transition_from_retro(retro_succeeded=False)
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_RETRO_FAILED


# --------------------------------------------------------------------------
# Convenience helpers: next_phase_from_verdict, is_terminal_phase,
# is_terminal_status, is_known_phase.
# --------------------------------------------------------------------------


class TestNextPhaseFromVerdict:
    """Dispatch helper for verdict-driven phases."""

    def test_dispatches_plan_correctly(self) -> None:
        result = pt.next_phase_from_verdict(pt.PHASE_PLANNING, {})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_RALPH

    def test_dispatches_ralph_correctly(self) -> None:
        result = pt.next_phase_from_verdict(pt.PHASE_RALPH, {"verdict": "SHIP"})
        assert result.next_phase == pt.PHASE_SUMMARY

    def test_dispatches_summary_correctly(self) -> None:
        result = pt.next_phase_from_verdict(pt.PHASE_SUMMARY, {})
        assert result.next_phase == pt.PHASE_PUSH_AND_PR

    def test_dispatches_push_and_pr_correctly(self) -> None:
        result = pt.next_phase_from_verdict(pt.PHASE_PUSH_AND_PR, {})
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_dispatches_fix_ci_correctly(self) -> None:
        result = pt.next_phase_from_verdict(pt.PHASE_FIX_CI, {"verdict": "PATCHED"})
        assert result.next_phase == pt.PHASE_AWAITING_CI

    def test_dispatches_fix_conflict_correctly(self) -> None:
        """#3225 — fix_conflict is dispatch-table wired for RESOLVED."""
        result = pt.next_phase_from_verdict(
            pt.PHASE_FIX_CONFLICT, {"verdict": "resolved"}
        )
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_PUSH_AND_PR

    def test_dispatches_fix_conflict_unresolvable(self) -> None:
        result = pt.next_phase_from_verdict(
            pt.PHASE_FIX_CONFLICT,
            {"verdict": "unresolvable", "resolution_notes": "reason"},
        )
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.failure_hint == pt.FAILURE_HINT_CONFLICT_UNRESOLVABLE

    def test_dispatches_verify_correctly(self) -> None:
        result = pt.next_phase_from_verdict(pt.PHASE_VERIFY, {"verdict": "VERIFIED"})
        assert result.next_phase == pt.PHASE_DONE

    def test_non_verdict_phase_returns_unrecognized(self) -> None:
        # awaiting_ci needs a PR-status payload, not a phase-output.
        # The helper returns UNRECOGNIZED so the caller knows to use
        # transition_from_awaiting_ci directly.
        result = pt.next_phase_from_verdict(pt.PHASE_AWAITING_CI, {})
        assert result.action == pt.TransitionAction.UNRECOGNIZED

    def test_unknown_phase_returns_unrecognized(self) -> None:
        result = pt.next_phase_from_verdict("never_existed", {})
        assert result.action == pt.TransitionAction.UNRECOGNIZED
        assert "never_existed" in result.reason


class TestTerminalPhaseAndStatus:
    """Terminal-phase and terminal-status predicates."""

    def test_is_terminal_phase_true_for_done(self) -> None:
        assert pt.is_terminal_phase(pt.PHASE_DONE) is True
        assert pt.is_terminal_phase(pt.PHASE_RETRO_DONE) is True
        assert pt.is_terminal_phase(pt.PHASE_RETRO_FAILED) is True
        assert pt.is_terminal_phase(pt.PHASE_NO_OP) is True
        assert pt.is_terminal_phase(pt.PHASE_CLEANUP_DONE) is True
        assert pt.is_terminal_phase(pt.PHASE_CLEANUP_BLOCKED) is True
        assert pt.is_terminal_phase(pt.PHASE_PLAN_BLOCKED) is True
        assert pt.is_terminal_phase(pt.PHASE_FORCE_STOPPED) is True
        assert pt.is_terminal_phase(pt.PHASE_DAEMON_RESTART_ABANDONED) is True
        assert pt.is_terminal_phase(pt.PHASE_PAUSED_BY_KILLSWITCH) is True

    def test_is_terminal_phase_true_for_agent_runner_post_pr_terminals(
        self,
    ) -> None:
        """#3176 — agent-runner post-ralph failure terminals."""
        assert pt.is_terminal_phase(pt.PHASE_AWAITING_CI_FAILED) is True
        assert pt.is_terminal_phase(pt.PHASE_AWAITING_CI_TIMEOUT) is True
        assert pt.is_terminal_phase(pt.PHASE_MERGE_FAILED) is True
        assert pt.is_terminal_phase(pt.PHASE_AWAITING_DEPLOY_FAILED) is True
        assert pt.is_terminal_phase(pt.PHASE_AWAITING_DEPLOY_TIMEOUT) is True

    def test_is_terminal_phase_true_for_conflict_unresolvable(self) -> None:
        """#3225 — fix_conflict budget-exhausted / unresolvable terminal."""
        assert pt.is_terminal_phase(pt.PHASE_CONFLICT_UNRESOLVABLE) is True

    def test_fix_conflict_is_intermediate_not_terminal(self) -> None:
        """#3225 — fix_conflict is active, not terminal (it advances)."""
        assert pt.is_terminal_phase(pt.PHASE_FIX_CONFLICT) is False

    def test_is_terminal_phase_false_for_intermediate(self) -> None:
        assert pt.is_terminal_phase(pt.PHASE_PLANNING) is False
        assert pt.is_terminal_phase(pt.PHASE_RALPH) is False
        assert pt.is_terminal_phase(pt.PHASE_SUMMARY) is False
        assert pt.is_terminal_phase(pt.PHASE_PUSH_AND_PR) is False
        assert pt.is_terminal_phase(pt.PHASE_AWAITING_CI) is False
        assert pt.is_terminal_phase(pt.PHASE_FIX_CI) is False
        assert pt.is_terminal_phase(pt.PHASE_MERGE) is False
        assert pt.is_terminal_phase(pt.PHASE_AWAITING_DEPLOY) is False
        assert pt.is_terminal_phase(pt.PHASE_VERIFY) is False
        assert pt.is_terminal_phase(pt.PHASE_RETRO) is False

    def test_is_terminal_phase_false_for_unknown(self) -> None:
        # Unknown phase strings are not terminal — the agent-runner
        # should keep looping (eventually crashing on unrecognized
        # verdict, which is fail-loud enough).
        assert pt.is_terminal_phase("never_existed") is False

    def test_is_terminal_status_true_for_terminal(self) -> None:
        assert pt.is_terminal_status("succeeded") is True
        assert pt.is_terminal_status("failed") is True
        assert pt.is_terminal_status("crashed") is True
        assert pt.is_terminal_status("plan_blocked") is True
        assert pt.is_terminal_status("needs_review") is True

    def test_is_terminal_status_false_for_active(self) -> None:
        assert pt.is_terminal_status("running") is False
        assert pt.is_terminal_status("retrying") is False

    def test_is_known_phase_happy_path(self) -> None:
        for phase in (
            pt.PHASE_CLAIMING,
            pt.PHASE_PLANNING,
            pt.PHASE_SETUP,
            pt.PHASE_RALPH,
            pt.PHASE_SUMMARY,
            pt.PHASE_PUSH_AND_PR,
            pt.PHASE_AWAITING_CI,
            pt.PHASE_FIX_CI,
            pt.PHASE_FIX_CONFLICT,  # #3225
            pt.PHASE_MERGE,
            pt.PHASE_AWAITING_DEPLOY,
            pt.PHASE_VERIFY,
            pt.PHASE_RETRO,
            pt.PHASE_DONE,
            pt.PHASE_RETRO_DONE,
            pt.PHASE_PLAN_BLOCKED,
            pt.PHASE_CONFLICT_UNRESOLVABLE,  # #3225
        ):
            assert pt.is_known_phase(phase), f"{phase!r} should be known"

    def test_is_known_phase_false_for_typo(self) -> None:
        assert pt.is_known_phase("plannnning") is False
        assert pt.is_known_phase("") is False


class TestVerdictExtraction:
    """Defensive extraction of verdict strings from phase outputs."""

    def test_verdict_uppercased(self) -> None:
        # ``transition_from_ralph`` with a lowercase 'ship' should
        # still advance — daemon.py uses the same upper()-normalize
        # pattern.
        result = pt.transition_from_ralph({"verdict": "ship"})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_SUMMARY

    def test_verdict_stripped(self) -> None:
        # Trailing whitespace in a verdict string (defensive — the
        # skills don't emit these today, but serialization quirks
        # could).
        result = pt.transition_from_ralph({"verdict": " SHIP "})
        assert result.action == pt.TransitionAction.ADVANCE
        assert result.next_phase == pt.PHASE_SUMMARY

    def test_non_string_verdict_handled_gracefully(self) -> None:
        # An int or bool in the verdict field should not crash —
        # ``str(x).upper()`` converts safely, and the non-matching
        # value routes to the non-SHIP diagnoser path.
        result = pt.transition_from_ralph({"verdict": 42})
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER
        assert result.context["verdict"] == "42"

    def test_none_verdict_handled_gracefully(self) -> None:
        # Explicit None verdict field — same path as missing field.
        result = pt.transition_from_ralph({"verdict": None})
        assert result.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER


class TestModuleContractInvariants:
    """Higher-level properties that catch drift between functions."""

    def test_every_active_phase_has_transition_or_is_mechanical(
        self,
    ) -> None:
        # Every phase in ACTIVE_PHASES either has a verdict-driven
        # transition OR is handled by a phase-specific function with
        # a different signature. A new phase added to ACTIVE_PHASES
        # with no transition function is a latent bug — this test
        # surfaces it.
        phase_specific = {
            pt.PHASE_CLAIMING,  # no LLM output; scheduler opens the agent row
            pt.PHASE_SETUP,  # mechanical; daemon does inline today
            pt.PHASE_AWAITING_CI,  # transition_from_awaiting_ci (different sig)
            pt.PHASE_MERGE,  # transition_from_merge (different sig)
            pt.PHASE_AWAITING_DEPLOY,  # transition_from_awaiting_deploy
            pt.PHASE_RETRO,  # transition_from_retro (different sig)
        }
        for phase in pt.ACTIVE_PHASES:
            if phase in phase_specific:
                continue
            # Must be in the verdict-driven dispatch table.
            result = pt.next_phase_from_verdict(phase, {"verdict": "SHIP"})
            assert result.action != pt.TransitionAction.UNRECOGNIZED, (
                f"phase {phase!r} has no registered verdict-driven transition"
            )

    def test_active_phase_names_distinct_from_terminal_phase_names(self) -> None:
        # A phase cannot be both active AND terminal — that would
        # break the "keep advancing until we hit terminal" loop
        # invariant. The two sets must be disjoint.
        overlap = pt.ACTIVE_PHASES & pt.TERMINAL_PHASES
        assert overlap == set(), (
            f"phase(s) appear in both active and terminal sets: {overlap}"
        )

    def test_transition_results_have_next_phase_xor_unrecognized(self) -> None:
        # ADVANCE + ADVANCE_WITH_STATUS must have a next_phase.
        # ROUTE_TO_DIAGNOSER + UNRECOGNIZED must not.
        # Sanity-check the most-used paths.
        advance_cases = [
            pt.transition_from_plan({}),
            pt.transition_from_ralph({"verdict": "SHIP"}),
            pt.transition_from_summary({}),
            pt.transition_from_push_and_pr({}),
            pt.transition_from_push_and_pr({"no_op": True}),
            pt.transition_from_awaiting_ci(
                {
                    "statusCheckRollup": [
                        {"status": "COMPLETED", "conclusion": "SUCCESS"}
                    ],
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                }
            ),
            pt.transition_from_fix_ci({"verdict": "PATCHED"}),
            pt.transition_from_fix_ci({"verdict": "FLAKY"}),
            pt.transition_from_merge(merge_succeeded=True),
            pt.transition_from_awaiting_deploy(deploy_succeeded=True),
            pt.transition_from_verify({"verdict": "VERIFIED"}),
            pt.transition_from_retro(retro_succeeded=True),
        ]
        for t in advance_cases:
            assert t.next_phase is not None, f"advance case has no next_phase: {t}"
            assert t.action in (
                pt.TransitionAction.ADVANCE,
                pt.TransitionAction.ADVANCE_WITH_STATUS,
            ), f"advance case has wrong action: {t}"

        diagnoser_cases = [
            pt.transition_from_ralph({"verdict": "REVISE"}),
            pt.transition_from_ralph({"verdict": "AC_INFEASIBLE"}),
            pt.transition_from_summary({"verdict": "AC_INFEASIBLE"}),
            pt.transition_from_fix_ci({"verdict": "BLOCKED"}),
            pt.transition_from_verify({"verdict": "FAILED"}),
            pt.transition_from_awaiting_deploy(deploy_succeeded=False),
        ]
        for t in diagnoser_cases:
            assert t.action == pt.TransitionAction.ROUTE_TO_DIAGNOSER, (
                f"diagnoser case has wrong action: {t}"
            )
            assert t.failure_hint, f"diagnoser case missing failure_hint: {t}"


class TestFrozenImmutability:
    """PhaseTransition is a frozen dataclass — mutation should raise."""

    def test_transition_is_frozen(self) -> None:
        t = pt.transition_from_plan({})
        import dataclasses

        with pytest_raises_frozen_instance():
            # Using a context manager means dataclasses.FrozenInstanceError
            # is what should propagate.
            t.next_phase = "mutated"  # type: ignore[misc]
        # Sanity-check with dataclasses.fields — just to make sure
        # the dataclass definition didn't drift.
        names = {f.name for f in dataclasses.fields(t)}
        assert "action" in names
        assert "next_phase" in names


def pytest_raises_frozen_instance():
    """Small shim: unittest-style context manager expecting the
    ``dataclasses.FrozenInstanceError`` raised by assignment to a
    frozen dataclass. Written locally so this test module does not
    need to import pytest.raises directly — keeps the module
    pytest-compatible but also runnable as a plain unittest.
    """
    import dataclasses

    class _Ctx:
        def __enter__(self) -> None:
            return None

        def __exit__(
            self, exc_type: type | None, exc: BaseException | None, tb: object
        ) -> bool:
            if exc_type is None:
                raise AssertionError("expected FrozenInstanceError; none raised")
            return issubclass(exc_type, dataclasses.FrozenInstanceError)

    return _Ctx()
