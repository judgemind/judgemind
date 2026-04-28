"""Contract tests verifying expected text patterns in skill SKILL.md files.

These tests assert that:

* ``prior_attempts.md`` is referenced in ``task-v2-ralph/SKILL.md`` Step 0.5
  and the worker-context handoff (AC b.3).
* ``## Prior attempts`` is referenced in ``ralph/SKILL.md`` worker prompt AND
  Claude-reviewer prompt (AC b.4).
* ``iteration_feedback.py`` and ``## Iteration feedback`` appear in
  ``task-v2-ralph/SKILL.md`` Step 4 (AC a.1 grep half).
* ``AC_INFEASIBLE`` verdict + ``infeasible_acs`` array are documented in
  ``task-v2-ralph/SKILL.md`` (issue #3010).
* ``AC_INFEASIBLE`` + positive triggers + negative guardrails appear in
  ``ralph/SKILL.md`` (issue #3010).
* ``deferred_acs``, ``infeasible_acs``, and classifier ordering appear in
  ``task-v2-summary/SKILL.md`` (issue #3010).
* ``deferred_acs`` + per-AC deferred labeling appear in
  ``task-v2-verify/SKILL.md`` (issue #3010).
* Per-category sections for ``ralph_ac_infeasible`` and
  ``summary_ac_infeasible``, the default-action vocabulary, and the
  wholesale-replace requirement for ``new_scope`` all appear in
  ``diagnose-failure/SKILL.md`` (issue #3010).

These are grep-style assertions against the literal SKILL.md text — they catch
regressions where an edit removes or renames a required marker.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # worktree root

_TASK_V2_RALPH_SKILL = _REPO_ROOT / ".claude" / "skills" / "task-v2-ralph" / "SKILL.md"
_RALPH_SKILL = _REPO_ROOT / ".claude" / "skills" / "ralph" / "SKILL.md"
_TASK_V2_SUMMARY_SKILL = (
    _REPO_ROOT / ".claude" / "skills" / "task-v2-summary" / "SKILL.md"
)
_TASK_V2_VERIFY_SKILL = (
    _REPO_ROOT / ".claude" / "skills" / "task-v2-verify" / "SKILL.md"
)
_TASK_V2_PLAN_SKILL = _REPO_ROOT / ".claude" / "skills" / "task-v2-plan" / "SKILL.md"
_TASK_V2_RETRO_SKILL = _REPO_ROOT / ".claude" / "skills" / "task-v2-retro" / "SKILL.md"
_DIAGNOSE_FAILURE_SKILL = (
    _REPO_ROOT / ".claude" / "skills" / "diagnose-failure" / "SKILL.md"
)


def _read(path: Path) -> str:
    assert path.exists(), f"SKILL.md not found: {path}"
    return path.read_text(encoding="utf-8")


class TestTaskV2RalphSkillContracts:
    """Contracts for ``.claude/skills/task-v2-ralph/SKILL.md``."""

    def test_prior_attempts_md_in_step_0_5(self) -> None:
        """Step 0.5 must reference ``prior_attempts.md`` (AC b.3)."""
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "prior_attempts.md" in content, (
            "task-v2-ralph/SKILL.md must reference prior_attempts.md (AC b.3)"
        )

    def test_step_0_5_heading_present(self) -> None:
        """The Step 0.5 heading must be present."""
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "Step 0.5" in content, (
            "task-v2-ralph/SKILL.md must have a Step 0.5 section for prior_attempts context"
        )

    def test_prior_attempts_section_in_task_md_format(self) -> None:
        """The task.md format in Step 1 must include ``## Prior attempts`` (AC b.3)."""
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "## Prior attempts" in content, (
            "task-v2-ralph/SKILL.md Step 1 must include ## Prior attempts "
            "in the task.md format (AC b.3)"
        )

    def test_iteration_feedback_py_in_step_4(self) -> None:
        """Step 4 must reference ``iteration_feedback.py`` (AC a.1 grep half)."""
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "iteration_feedback.py" in content, (
            "task-v2-ralph/SKILL.md Step 4 must reference iteration_feedback.py (AC a.1)"
        )

    def test_iteration_feedback_heading_in_step_4(self) -> None:
        """Step 4 must reference ``## Iteration feedback`` output (AC a.1 grep half)."""
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "## Iteration feedback" in content, (
            "task-v2-ralph/SKILL.md must reference ## Iteration feedback (AC a.1)"
        )


class TestRalphSkillContracts:
    """Contracts for ``.claude/skills/ralph/SKILL.md``."""

    def test_prior_attempts_in_worker_prompt(self) -> None:
        """Worker prompt must reference ``## Prior attempts`` (AC b.4)."""
        content = _read(_RALPH_SKILL)
        assert "## Prior attempts" in content, (
            "ralph/SKILL.md must reference ## Prior attempts in worker prompt (AC b.4)"
        )

    def test_prior_attempts_in_claude_reviewer_prompt(self) -> None:
        """Claude reviewer prompt must reference prior attempts context (AC b.4)."""
        content = _read(_RALPH_SKILL)
        # The reviewer prompt section starts at "Claude reviewer prompt".
        reviewer_idx = content.find("Claude reviewer prompt")
        assert reviewer_idx != -1, (
            "ralph/SKILL.md must have a Claude reviewer prompt section"
        )
        reviewer_section = content[reviewer_idx:]
        assert (
            "Prior attempts" in reviewer_section or "prior attempts" in reviewer_section
        ), (
            "ralph/SKILL.md Claude reviewer prompt must reference prior attempts (AC b.4)"
        )

    def test_prior_attempts_in_task_md_section_list(self) -> None:
        """Step 0 task.md section list must mention ``## Prior attempts (optional)``."""
        content = _read(_RALPH_SKILL)
        assert "## Prior attempts (optional)" in content, (
            "ralph/SKILL.md Step 0 must list ## Prior attempts (optional) in task.md sections"
        )

    def test_prior_attempts_file_path_referenced(self) -> None:
        """ralph/SKILL.md must reference the file path ``prior_attempts.md``."""
        content = _read(_RALPH_SKILL)
        assert "prior_attempts.md" in content, (
            "ralph/SKILL.md must reference prior_attempts.md so workers know where to look"
        )


# ------------------------------------------------------------------
# Issue #3010 — AC_INFEASIBLE + deferred_acs contract coverage.
# ------------------------------------------------------------------


class TestTaskV2RalphAcInfeasibleContracts:
    """task-v2-ralph/SKILL.md must document the AC_INFEASIBLE verdict
    and ``infeasible_acs`` array shape (issue #3010 AC #1)."""

    def test_ac_infeasible_verdict_documented(self) -> None:
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "AC_INFEASIBLE" in content, (
            "task-v2-ralph/SKILL.md must document the AC_INFEASIBLE verdict "
            "(issue #3010)"
        )

    def test_infeasible_acs_array_documented(self) -> None:
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "infeasible_acs" in content, (
            "task-v2-ralph/SKILL.md must document the infeasible_acs array "
            "(issue #3010)"
        )
        # The normative shape must mention ``index`` and ``evidence``.
        assert "index" in content and "evidence" in content, (
            "task-v2-ralph/SKILL.md infeasible_acs shape must name "
            "index + evidence fields"
        )

    def test_ralph_ac_infeasible_failure_category_referenced(self) -> None:
        """Output-contract prose must reference the daemon's
        ``ralph_ac_infeasible`` failure category so the skill author
        and the daemon author stay in sync on the routing."""
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "ralph_ac_infeasible" in content, (
            "task-v2-ralph/SKILL.md must reference the ralph_ac_infeasible "
            "failure category (issue #3010)"
        )


class TestRalphAcInfeasibleContracts:
    """ralph/SKILL.md must describe positive triggers AND negative
    guardrails for AC_INFEASIBLE (issue #3010 AC #2)."""

    def test_ac_infeasible_emit_rules_section(self) -> None:
        content = _read(_RALPH_SKILL)
        assert "AC_INFEASIBLE emit rules" in content, (
            'ralph/SKILL.md must have a §"AC_INFEASIBLE emit rules" section '
            "(issue #3010)"
        )

    def test_positive_triggers_listed(self) -> None:
        content = _read(_RALPH_SKILL)
        # All three positive trigger kinds named in the scope section.
        assert "Non-existent symbol" in content
        assert "Self-contradiction" in content
        assert "Out-of-scope dependency" in content

    def test_negative_guardrails_listed(self) -> None:
        """The guardrail rules must be explicit so the worker does not
        raise AC_INFEASIBLE for legitimate failure modes."""
        content = _read(_RALPH_SKILL)
        # Exact phrases from the guardrail list — renaming is OK but
        # the concept must remain locked into the spec.
        assert "Hard to implement" in content, (
            "ralph/SKILL.md must explicitly guard against 'Hard to implement' "
            "as an AC_INFEASIBLE trigger"
        )
        assert "Max iterations exhausted" in content, (
            "ralph/SKILL.md must explicitly guard against 'Max iterations "
            "exhausted' as an AC_INFEASIBLE trigger (it is ralph_max_iterations)"
        )

    def test_worker_prompt_mentions_ac_infeasible(self) -> None:
        content = _read(_RALPH_SKILL)
        # The testable worker prompt must instruct workers about the emit
        # path — look for the work-status.txt contract change.
        assert "AC_INFEASIBLE" in content and "work-status.txt" in content, (
            "ralph/SKILL.md worker prompt must instruct workers to write "
            "AC_INFEASIBLE to work-status.txt"
        )

    def test_claude_reviewer_prompt_has_ac_infeasible_branch(self) -> None:
        content = _read(_RALPH_SKILL)
        reviewer_idx = content.find("Claude reviewer prompt")
        assert reviewer_idx != -1
        reviewer_section = content[reviewer_idx:]
        # The reviewer's three-way decision now includes AC_INFEASIBLE.
        assert "AC_INFEASIBLE" in reviewer_section, (
            "ralph/SKILL.md Claude reviewer prompt must document the "
            "AC_INFEASIBLE branch (issue #3010)"
        )


class TestTaskV2SummaryContracts:
    """task-v2-summary/SKILL.md must document the extended output shape
    (deferred_acs, infeasible_acs, unmet_criteria) and the classifier
    order (deferred → validate → unmet → classify) per issue #3010."""

    def test_deferred_acs_documented(self) -> None:
        content = _read(_TASK_V2_SUMMARY_SKILL)
        assert "deferred_acs" in content, (
            "task-v2-summary/SKILL.md must document deferred_acs (issue #3010)"
        )

    def test_infeasible_acs_documented(self) -> None:
        content = _read(_TASK_V2_SUMMARY_SKILL)
        assert "infeasible_acs" in content, (
            "task-v2-summary/SKILL.md must document infeasible_acs (issue #3010)"
        )

    def test_classifier_keyword_present(self) -> None:
        content = _read(_TASK_V2_SUMMARY_SKILL)
        assert "classifier" in content.lower(), (
            "task-v2-summary/SKILL.md must describe the classifier (issue #3010)"
        )

    def test_classifier_order_deferred_first(self) -> None:
        """The deferred check runs BEFORE the validate-against-diff check."""
        content = _read(_TASK_V2_SUMMARY_SKILL)
        # Verbatim section titles from the new classifier ordering.
        assert "Deferred check (runs FIRST" in content, (
            "task-v2-summary/SKILL.md classifier must put the deferred "
            "check FIRST (issue #3010)"
        )

    def test_shape_mismatch_vs_structural_impossibility(self) -> None:
        """Summary must distinguish shape-mismatch (needs_review) from
        structural-impossibility (AC_INFEASIBLE)."""
        content = _read(_TASK_V2_SUMMARY_SKILL)
        assert "Shape mismatch" in content or "shape-mismatch" in content
        assert (
            "Structural impossibility" in content
            or "structural impossibility" in content
        )

    def test_summary_ac_infeasible_failure_category_referenced(self) -> None:
        content = _read(_TASK_V2_SUMMARY_SKILL)
        assert "summary_ac_infeasible" in content, (
            "task-v2-summary/SKILL.md must reference the "
            "summary_ac_infeasible failure category (issue #3010)"
        )

    def test_verdict_field_documented(self) -> None:
        """The output JSON's ``verdict`` field must be documented."""
        content = _read(_TASK_V2_SUMMARY_SKILL)
        # Match the output-JSON snippet literal.
        assert '"verdict": "OK" | "AC_INFEASIBLE"' in content, (
            "task-v2-summary/SKILL.md output contract must show "
            "verdict: OK | AC_INFEASIBLE"
        )


class TestTaskV2VerifyContracts:
    """task-v2-verify/SKILL.md must describe reading deferred_acs from
    summary and labeling per-AC evidence as deferred vs pre-merge
    (issue #3010)."""

    def test_deferred_acs_input_documented(self) -> None:
        content = _read(_TASK_V2_VERIFY_SKILL)
        assert "deferred_acs" in content, (
            "task-v2-verify/SKILL.md must document reading deferred_acs (issue #3010)"
        )

    def test_deferred_labeling_vocabulary(self) -> None:
        """The evidence comment distinguishes deferred-marker from
        deferred-heuristic from pre-merge-belt."""
        content = _read(_TASK_V2_VERIFY_SKILL)
        assert "deferred (marker)" in content, (
            "task-v2-verify/SKILL.md must use the 'deferred (marker)' label"
        )
        assert "deferred (heuristic)" in content, (
            "task-v2-verify/SKILL.md must use the 'deferred (heuristic)' label"
        )
        assert "pre-merge validated" in content, (
            "task-v2-verify/SKILL.md must use the 'pre-merge validated' label "
            "for non-deferred ACs re-confirmed post-deploy"
        )


class TestDiagnoseFailureContracts:
    """diagnose-failure/SKILL.md must have per-category sections for
    ralph_ac_infeasible and summary_ac_infeasible, plus the
    wholesale-replace requirement for new_scope (issue #3010)."""

    def test_ralph_ac_infeasible_section(self) -> None:
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        assert "ralph_ac_infeasible" in content, (
            "diagnose-failure/SKILL.md must have a ralph_ac_infeasible "
            "section (issue #3010)"
        )

    def test_summary_ac_infeasible_section(self) -> None:
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        assert "summary_ac_infeasible" in content, (
            "diagnose-failure/SKILL.md must have a summary_ac_infeasible "
            "section (issue #3010)"
        )

    def test_default_action_vocabulary(self) -> None:
        """The three default actions must all appear in the
        per-category guidance."""
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        for action in ("reissue", "escalate", "close"):
            assert action in content, (
                f"diagnose-failure/SKILL.md must name the '{action}' action "
                "(issue #3010)"
            )

    def test_new_scope_wholesale_replace_requirement(self) -> None:
        """``new_scope`` is always the complete rewritten issue body —
        not a diff, not a patch, not a partial splice."""
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        # The SKILL.md uses "wholesale replace" or "complete rewritten
        # issue body" (or both) — lock on either phrase.
        assert (
            "wholesale" in content.lower()
            or "complete rewritten issue body" in content.lower()
            or "complete, well-formed issue body" in content.lower()
        ), (
            "diagnose-failure/SKILL.md must document the wholesale-replace "
            "requirement for new_scope (issue #3010)"
        )

    # ------------------------------------------------------------------
    # Issue #3057 — anchor-bias defense contracts
    # ------------------------------------------------------------------

    def test_step_1_parse_failure_signature_section(self) -> None:
        """SKILL.md must have a "parse the failure signature" step that
        runs BEFORE consulting prior decisions (issue #3057)."""
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        # The step heading. Allow either "Step 1" or "parse the failure
        # signature" in a heading — the contract is that the first
        # procedural step is to parse the signature, not consult priors.
        lower = content.lower()
        assert "parse the failure signature" in lower, (
            "diagnose-failure/SKILL.md must have a "
            "'parse the failure signature' step (issue #3057)"
        )

    def test_ground_truth_vs_priors_framing(self) -> None:
        """SKILL.md must frame stderr as ground truth and prior decisions
        as priors (not evidence). This is the core framing that defuses
        the anchor-bias failure mode."""
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        lower = content.lower()
        assert "stderr is ground truth" in lower, (
            "diagnose-failure/SKILL.md must frame the stderr as "
            "'ground truth' (issue #3057)"
        )
        assert "priors, not evidence" in lower, (
            "diagnose-failure/SKILL.md must frame prior decisions as "
            "'priors, not evidence' (issue #3057)"
        )

    def test_verbatim_quote_requirement(self) -> None:
        """SKILL.md must require the diagnoser to quote the actual stderr
        failure line verbatim in the ``reasoning`` field."""
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        lower = content.lower()
        # The verbatim-quote language: must mention both "verbatim" and
        # "reasoning" (the field where the quote lands).
        assert "verbatim" in lower, (
            "diagnose-failure/SKILL.md must mention 'verbatim' quoting "
            "of the stderr failure line (issue #3057)"
        )
        assert "reasoning" in lower, (
            "diagnose-failure/SKILL.md must describe where the verbatim "
            "stderr quote lands (the ``reasoning`` field — issue #3057)"
        )

    def test_3057_issue_reference(self) -> None:
        """SKILL.md must reference issue #3057 so future readers can
        find the anchor-bias forensic."""
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        assert "#3057" in content, (
            "diagnose-failure/SKILL.md must reference issue #3057 "
            "(the anchor-bias forensic that motivated §Step 1)"
        )

    def test_fleet_decisions_cap_documented(self) -> None:
        """SKILL.md must document the configurable cap on
        ``recent_fleet_decisions`` (issue #3057 — default 3, tunable
        via ``dispatcher.config.diagnoser_fleet_decisions_cap``)."""
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        assert "diagnoser_fleet_decisions_cap" in content, (
            "diagnose-failure/SKILL.md must document the tunable cap "
            "``dispatcher.config.diagnoser_fleet_decisions_cap`` "
            "(issue #3057)"
        )


# ------------------------------------------------------------------
# Issue #3017 — Per-skill heartbeat lines contract coverage.
# ------------------------------------------------------------------


class TestTaskV2SimpleSkillHeartbeats:
    """plan / summary / verify / retro each emit PHASE_START / PHASE_DONE
    heartbeat tokens to stdout so CloudWatch Log Insights can answer
    "what was the last thing the skill did before going silent?" during
    a hang (issue #3017)."""

    def test_plan_emits_phase_start_and_phase_done(self) -> None:
        content = _read(_TASK_V2_PLAN_SKILL)
        assert "PHASE_START plan" in content, (
            "task-v2-plan/SKILL.md must document the PHASE_START plan heartbeat "
            "(issue #3017)"
        )
        assert "PHASE_DONE" in content, (
            "task-v2-plan/SKILL.md must document the PHASE_DONE heartbeat (issue #3017)"
        )
        assert "#3017" in content

    def test_summary_emits_phase_start_and_phase_done(self) -> None:
        content = _read(_TASK_V2_SUMMARY_SKILL)
        assert "PHASE_START summary" in content, (
            "task-v2-summary/SKILL.md must document the PHASE_START summary "
            "heartbeat (issue #3017)"
        )
        assert "PHASE_DONE" in content
        assert "#3017" in content

    def test_verify_emits_phase_start_and_phase_done(self) -> None:
        content = _read(_TASK_V2_VERIFY_SKILL)
        assert "PHASE_START verify" in content
        assert "PHASE_DONE" in content
        assert "#3017" in content

    def test_retro_emits_phase_start_and_phase_done(self) -> None:
        content = _read(_TASK_V2_RETRO_SKILL)
        assert "PHASE_START retro" in content
        assert "PHASE_DONE" in content
        assert "#3017" in content


class TestTaskV2RalphHeartbeats:
    """Ralph is the long-tail phase where hangs actually happen. It
    emits a richer set of heartbeats: ITER_START, WORKER_START,
    REVIEWER_START, PYTEST_START, PUSH_START (issue #3017)."""

    def test_ralph_emits_iter_start(self) -> None:
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "ITER_START" in content, (
            "task-v2-ralph/SKILL.md must document the ITER_START heartbeat "
            "(issue #3017)"
        )

    def test_ralph_emits_worker_start(self) -> None:
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "WORKER_START" in content

    def test_ralph_emits_reviewer_start(self) -> None:
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "REVIEWER_START" in content

    def test_ralph_emits_pytest_start(self) -> None:
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "PYTEST_START" in content

    def test_ralph_documents_push_start(self) -> None:
        """PUSH_START is documented in ralph even though it fires on
        the daemon side — operators reading the ralph jsonl need to know
        what to expect and what NOT to expect."""
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "PUSH_START" in content

    def test_ralph_references_issue_3017(self) -> None:
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "#3017" in content, (
            "task-v2-ralph/SKILL.md must reference issue #3017 in the "
            "heartbeat section for traceability"
        )

    def test_ralph_references_stream_forwarder(self) -> None:
        """The heartbeat section must name the stream-forwarder module
        so operators know where CloudWatch tagging happens."""
        content = _read(_TASK_V2_RALPH_SKILL)
        assert "stream_forwarder.py" in content or "stream-forwarder" in content


# ------------------------------------------------------------------
# Issue #2966 — task-v2-fix-ci: rebase-first step + force-with-lease
# ------------------------------------------------------------------


class TestTaskV2FixCiSkillContracts:
    """Contract tests for ``.claude/skills/task-v2-fix-ci/SKILL.md``.

    These grep-style assertions catch regressions where an edit removes
    or renames a required marker introduced by issue #2966 (mandatory
    rebase + force-with-lease push).
    """

    _SKILL_PATH = _REPO_ROOT / ".claude" / "skills" / "task-v2-fix-ci" / "SKILL.md"

    def _content(self) -> str:
        return _read(self._SKILL_PATH)

    def test_rebase_first_step_present(self) -> None:
        """Step 0 must document the mandatory fetch + rebase commands
        (AC1, issue #2966).

        The exact form is ``git -C <worktree_path> fetch origin main`` and
        ``git -C <worktree_path> rebase origin/main`` — we check for the
        distinctive ``fetch origin main`` and ``rebase origin/main``
        substrings which are present in both the -C and plain forms.
        """
        content = self._content()
        assert "fetch origin main" in content, (
            "task-v2-fix-ci/SKILL.md must document a `fetch origin main` "
            "command in Step 0 (issue #2966 AC1)"
        )
        assert "rebase origin/main" in content, (
            "task-v2-fix-ci/SKILL.md must document a `rebase origin/main` "
            "command in Step 0 (issue #2966 AC1)"
        )

    def test_force_with_lease_documented(self) -> None:
        """The output contract must explain ``--force-with-lease`` semantics
        (AC2, issue #2966)."""
        content = self._content()
        assert "--force-with-lease" in content, (
            "task-v2-fix-ci/SKILL.md must document --force-with-lease "
            "push semantics (issue #2966 AC2)"
        )

    def test_rebase_outcome_field_documented(self) -> None:
        """The output contract must name the ``rebase_outcome`` field
        (issue #2966)."""
        content = self._content()
        assert "rebase_outcome" in content, (
            "task-v2-fix-ci/SKILL.md must document the rebase_outcome "
            "output field (issue #2966)"
        )

    def test_conflict_unresolvable_enum_value_documented(self) -> None:
        """The ``conflict_unresolvable`` enum value must appear in the
        output contract (issue #2966)."""
        content = self._content()
        assert "conflict_unresolvable" in content, (
            "task-v2-fix-ci/SKILL.md must document the conflict_unresolvable "
            "rebase_outcome value (issue #2966)"
        )

    def test_fix_ci_rebase_outcome_heartbeat_documented(self) -> None:
        """The skill must document the ``FIX_CI_REBASE_OUTCOME`` heartbeat
        line (AC6 observability, issue #2966)."""
        content = self._content()
        assert "FIX_CI_REBASE_OUTCOME" in content, (
            "task-v2-fix-ci/SKILL.md must document the FIX_CI_REBASE_OUTCOME "
            "heartbeat echo line (issue #2966 AC6)"
        )


# ------------------------------------------------------------------
# Issue #3725 — duplicate-PR consolidation example contract coverage.
# ------------------------------------------------------------------


class TestDuplicatePrConsolidationContract:
    """diagnose-failure/SKILL.md must document the duplicate-PR
    consolidation pattern (Example 8) as a regression guard (issue #3725)."""

    def test_duplicate_pr_example_present(self) -> None:
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        assert "duplicate-PR consolidation" in content, (
            "diagnose-failure/SKILL.md must contain 'duplicate-PR consolidation' "
            "example (issue #3725)"
        )

    def test_duplicate_pr_detection_command_present(self) -> None:
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        assert "in:body Closes" in content, (
            "diagnose-failure/SKILL.md must document the gh search pattern "
            "'in:body Closes' for duplicate-PR detection (issue #3725)"
        )

    def test_duplicate_pr_close_template_present(self) -> None:
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        assert "Closing as duplicate of" in content, (
            "diagnose-failure/SKILL.md must document the close-comment template "
            "'Closing as duplicate of' (issue #3725)"
        )

    def test_duplicate_pr_action_taken_present(self) -> None:
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        assert "consolidate_duplicate_prs" in content, (
            "diagnose-failure/SKILL.md must document 'consolidate_duplicate_prs' "
            "as the action_taken value (issue #3725)"
        )

    def test_duplicate_pr_3725_issue_reference(self) -> None:
        content = _read(_DIAGNOSE_FAILURE_SKILL)
        assert "#3725" in content, (
            "diagnose-failure/SKILL.md must reference '#3725' for traceability "
            "(issue #3725)"
        )
