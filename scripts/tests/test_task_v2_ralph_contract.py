"""Contract tests for the /task-v2-ralph and /ralph skill markdown files.

These tests validate the post-#2845 architectural contract:

- `/task-v2-ralph` no longer contains the change_type-based short-circuit
  that emitted `iterations_used=0, changed_files=[]` for
  `docs`/`db_migration`/`dx_tooling`/`no_deployed_component` (landed in
  #2767, superseded by #2845).
- `/ralph` contains a non-testable branch in both the worker prompt and
  the reviewer prompt, so pure docs/db_migration/dx_tooling diffs are
  actually implemented and reviewed without a "no tests added" REVISE.

Synthetic test coverage for the acceptance criteria:

- "plan with `change_type=docs` + empty worktree produces a ralph
  invocation with `iterations_used >= 1` and non-empty `changed_files`":
  validated by asserting the short-circuit emit-SHIP-with-zero-iterations
  path has been removed from the skill's Step 0.
- "plan with `change_type=dx_tooling` drives ralph to implement the
  plan's 'What will change' section with real commits": validated by
  asserting the non-testable worker prompt tells the worker to
  "implement the task directly" and targets the plan's
  "What will change" section.

Background: the short-circuit was a read-only-plan / skip-ralph
assumption that contradicted the plan skill's own read-only contract.
Every non-testable agent since Phase 3 cutover produced zero work
(#2832, #2831, #2712 on 2026-04-19). See #2845 for the full analysis.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_V2_RALPH_SKILL = REPO_ROOT / ".claude" / "skills" / "task-v2-ralph" / "SKILL.md"
RALPH_SKILL = REPO_ROOT / ".claude" / "skills" / "ralph" / "SKILL.md"


def _read_skill(path: Path) -> str:
    """Read a SKILL.md file and return its full text."""
    assert path.exists(), f"expected skill file to exist: {path}"
    return path.read_text(encoding="utf-8")


class TestTaskV2RalphShortCircuitRemoved:
    """The #2767 short-circuit must be removed by #2845."""

    def test_skill_file_exists(self) -> None:
        assert TASK_V2_RALPH_SKILL.exists(), (
            "/task-v2-ralph/SKILL.md must exist"
        )

    def test_no_step_0_change_type_routing_heading(self) -> None:
        """Step 0 is no longer 'Change-type routing' — it's the Task-tool
        availability check only.

        The pre-#2845 skill had a Step 0 titled 'Change-type routing
        (short-circuit for non-testable types)'. Its removal is the
        load-bearing signal that the short-circuit is gone.
        """
        content = _read_skill(TASK_V2_RALPH_SKILL)
        assert "short-circuit for non-testable types" not in content, (
            "Step 0 'short-circuit for non-testable types' must be removed "
            "(superseded by #2845 — see skill's Design decision section)."
        )
        assert "Change-type routing" not in content, (
            "Step 0 'Change-type routing' heading must be removed "
            "(superseded by #2845)."
        )

    def test_no_short_circuit_implementation_block(self) -> None:
        """The JSON emit-SHIP-with-zero-iterations short-circuit body
        must not appear in the skill.

        The pre-#2845 skill had a 'Short-circuit implementation' section
        that emitted a SHIP verdict with `iterations_used: 0` and
        `changed_files: []` for non-testable change types. Both markers
        must be gone.
        """
        content = _read_skill(TASK_V2_RALPH_SKILL)
        assert "Short-circuit implementation" not in content, (
            "The 'Short-circuit implementation' section must be removed "
            "(superseded by #2845)."
        )
        # The substring `"iterations_used": 0` appears legitimately in
        # the Task-tool BLOCKED path and in worked examples for the
        # BLOCKED case. But the non-testable SHIP-with-zero-iterations
        # pattern (SHIP + iterations_used 0 + "Skipped — change_type=")
        # was the broken path. Its marker phrase is unique enough to
        # match exactly.
        assert "Skipped — change_type=" not in content, (
            "The non-testable SHIP short-circuit's summary phrase "
            "'Skipped — change_type=' must be removed (superseded by #2845)."
        )
        assert "does not need test-driven iteration" not in content, (
            "The non-testable short-circuit's rationale phrase "
            "'does not need test-driven iteration' must be removed "
            "(superseded by #2845). The inner /ralph skill now adapts "
            "per change_type instead."
        )

    def test_design_decision_section_records_supersession(self) -> None:
        """The skill must explicitly record that #2767 was superseded by
        #2845.

        The Design decision section is the architectural provenance
        trail — without it, a future reader would re-litigate the
        routing debate.
        """
        content = _read_skill(TASK_V2_RALPH_SKILL)
        assert "#2767" in content, (
            "The skill must reference #2767 in its Design decision section "
            "(supersession record)."
        )
        assert "#2845" in content, (
            "The skill must reference #2845 in its Design decision section."
        )
        assert "superseded" in content.lower() or "supersedes" in content.lower(), (
            "The skill must contain the word 'superseded' or 'supersedes' "
            "in its Design decision section."
        )

    def test_step_0_is_task_tool_check(self) -> None:
        """Step 0 in the post-#2845 skill is the Task-tool availability
        check (previously Step 0.5).

        The short-circuit occupied Step 0 before #2845; Step 0.5 was
        the Task-tool check. With the short-circuit removed, the
        Task-tool check becomes Step 0.
        """
        content = _read_skill(TASK_V2_RALPH_SKILL)
        # Step 0 heading should be present and refer to Task-tool.
        assert "## Step 0 — Task-tool availability check" in content, (
            "Step 0 heading must be renamed to 'Task-tool availability "
            "check' after the short-circuit removal."
        )

    def test_ralph_runs_for_every_change_type_statement(self) -> None:
        """The skill must contain an explicit statement that ralph runs
        for every change type.

        Without this, a future reader might re-add a short-circuit for
        the "new" non-testable type they're introducing.
        """
        content = _read_skill(TASK_V2_RALPH_SKILL)
        assert "Ralph runs for every change type" in content, (
            "The skill must contain the explicit statement 'Ralph runs "
            "for every change type' — this is the anti-regression "
            "signal for the #2845 fix."
        )

    def test_iterations_used_contract_is_one_or_more(self) -> None:
        """The output contract must state `iterations_used` is 1 or more
        when ralph runs (not 0).

        The pre-#2845 contract allowed `iterations_used=0` when the
        short-circuit fired. Post-fix, 0 is only valid on the BLOCKED
        Task-tool-unavailable path.
        """
        content = _read_skill(TASK_V2_RALPH_SKILL)
        # Check the output-contract range.
        assert "1..max_iterations" in content, (
            "The output contract must specify `iterations_used` in "
            "1..max_iterations (0 is only valid for BLOCKED paths)."
        )


class TestRalphNonTestableBranch:
    """The /ralph skill must have explicit non-testable worker +
    reviewer branches after #2845."""

    def test_skill_file_exists(self) -> None:
        assert RALPH_SKILL.exists(), "/ralph/SKILL.md must exist"

    def test_change_type_aware_behavior_section(self) -> None:
        """The skill must document its change-type-aware behavior at
        the top so callers know ralph adapts to testable vs
        non-testable."""
        content = _read_skill(RALPH_SKILL)
        assert "Change-type-aware behavior" in content, (
            "/ralph must document its change-type-aware behavior in a "
            "top-level section (required by #2845 for caller visibility)."
        )
        assert "## Testable" in content, (
            "/ralph must reference the `## Testable` task.md line as the "
            "branch signal."
        )

    def test_non_testable_change_types_enumerated(self) -> None:
        """The skill must enumerate the four non-testable change types.

        This is the same enumeration as `/task-v2-ralph` Step 1's
        taxonomy — the two must stay in sync.
        """
        content = _read_skill(RALPH_SKILL)
        for ct in ("docs", "db_migration", "dx_tooling", "no_deployed_component"):
            assert ct in content, (
                f"/ralph must list `{ct}` as a non-testable change type."
            )

    def test_non_testable_worker_prompt_present(self) -> None:
        """The worker prompt must have an explicit non-testable branch.

        Acceptance criterion: 'plan with `change_type=dx_tooling` drives
        ralph to implement the plan's "What will change" section'.
        """
        content = _read_skill(RALPH_SKILL)
        assert "Non-testable worker prompt" in content, (
            "/ralph must contain a 'Non-testable worker prompt' section — "
            "without it, the non-testable branch has nothing to run."
        )
        assert "What will change" in content, (
            "The non-testable worker prompt must direct the worker at "
            "the plan's 'What will change' section (AC #4 of #2845)."
        )
        assert "implement the task directly" in content, (
            "The non-testable worker prompt must tell the worker to "
            "'implement the task directly' (no TDD)."
        )

    def test_non_testable_worker_skips_tdd_and_pytest(self) -> None:
        """The non-testable worker prompt must explicitly skip pytest
        and diff-coverage gates.

        Running pytest on a docs-only diff wastes time and often fails
        on unrelated pre-existing test issues unrelated to the diff.
        """
        content = _read_skill(RALPH_SKILL)
        # Look for skip-pytest language in the non-testable section.
        # The exact marker phrase is 'Pytest and diff-coverage are **skipped**'.
        assert "Pytest and diff-coverage are **skipped**" in content, (
            "The non-testable worker prompt must explicitly skip pytest "
            "and diff-coverage."
        )

    def test_reviewer_skip_no_tests_added_on_non_testable(self) -> None:
        """The reviewer prompt must NOT flag 'no tests added' as a
        REVISE reason on the non-testable branch.

        This is the central behavior change from #2845's acceptance
        criteria: 'no spurious "no tests added" REVISE on a pure docs
        change.'
        """
        content = _read_skill(RALPH_SKILL)
        # The reviewer prompt has an explicit instruction not to REVISE
        # on missing tests for non-testable changes.
        assert 'do **not** flag "no tests added"' in content, (
            "The reviewer prompt must explicitly say: do not flag 'no "
            "tests added' as REVISE on non-testable changes."
        )
        assert 'Do not REVISE for "no tests added" on non-testable changes' in content, (
            "The Guardrails section must reinforce: 'no tests added' is "
            "not a REVISE reason on the non-testable branch."
        )

    def test_single_reviewer_pass_for_non_testable(self) -> None:
        """The non-testable branch must accept a single Claude reviewer
        pass (skip both Gemini reviews).

        Running Gemini code-review on a markdown-only diff produces no
        signal and wastes ~2-3 minutes of reviewer wall-clock.
        """
        content = _read_skill(RALPH_SKILL)
        assert "Single-reviewer phase (non-testable branch)" in content, (
            "/ralph must contain a 'Single-reviewer phase (non-testable "
            "branch)' section that runs only Claude."
        )
        assert "Skip the two Gemini reviews" in content, (
            "The non-testable single-reviewer phase must explicitly "
            "skip the two Gemini passes."
        )
        # The decision predicate should reduce to Claude-SHIP on non-
        # testable (since Gemini entries are SKIPPED placeholders).
        assert "reduces to Claude SHIP" in content, (
            "The decision logic must note the non-testable predicate "
            "reduces to Claude SHIP (uniform predicate across branches)."
        )

    def test_fail_open_when_testable_line_missing(self) -> None:
        """When the `## Testable` line is absent from task.md, ralph
        must fall back to the testable branch (preserves laptop-
        dispatcher `/task` behavior — it never wrote this line)."""
        content = _read_skill(RALPH_SKILL)
        # Look for the fail-open language.
        assert "missing" in content and "treat it as `yes`" in content, (
            "/ralph must fail-open to the testable branch when "
            "`## Testable` is missing (laptop-dispatcher compatibility)."
        )


class TestSyntheticDocsChangeType:
    """Synthetic AC check: `change_type=docs` + empty worktree must
    produce `iterations_used >= 1` and non-empty `changed_files`.

    We cannot invoke `claude -p /task-v2-ralph` from a unit test (no
    claude binary, no Task tool, no API key). Instead we validate the
    structural guarantee: there is no path in the skill that emits
    SHIP + iterations_used=0 + changed_files=[] for change_type=docs.
    """

    def test_no_short_circuit_for_docs(self) -> None:
        """Assert there is no 'if change_type == docs: emit SHIP with
        iterations 0' logic in the skill."""
        content = _read_skill(TASK_V2_RALPH_SKILL)
        # Scan for any block that combines 'docs' (in a change_type
        # context) with a zero-iteration SHIP. The broken pre-#2845
        # skill had a list of change types followed by a SHIP JSON
        # with iterations_used: 0.
        # After #2845, the only `iterations_used: 0` appears in the
        # BLOCKED Task-tool-unavailable path and in the BLOCKED worked
        # example. Those are unambiguous — BLOCKED, not SHIP.
        # Assert the skill never combines 'SHIP' verdict with the
        # non-testable rationale phrases.
        assert "does not need test-driven iteration" not in content
        assert "Skipped — change_type=" not in content

    def test_docs_change_type_still_reaches_ralph_loop(self) -> None:
        """Assert the skill's Step 1 (seed state directory) is invoked
        unconditionally — not gated on change_type."""
        content = _read_skill(TASK_V2_RALPH_SKILL)
        # Step 1 heading exists and seeds task.md with `## Change type`
        # and `## Testable` — not conditional on the value.
        assert "## Step 1 — Seed ralph state directory" in content, (
            "Step 1 must exist and run for every change type."
        )
        # Step 1 must write a `## Testable` line derived from
        # change_type. Both testable and non-testable enumerations must
        # be present.
        assert "## Testable" in content, (
            "Step 1 must seed a `## Testable` line in task.md."
        )
        # The testable enumeration must list all six testable types.
        for ct in ("api", "scraper", "ingestion", "web", "backfill_script", "agent_skill"):
            assert ct in content, (
                f"Step 1 must list `{ct}` as a testable change type."
            )


class TestSyntheticDxToolingChangeType:
    """Synthetic AC check: `change_type=dx_tooling` must drive ralph to
    implement the plan's 'What will change' section with real commits.

    We validate this structurally: the non-testable worker prompt in
    /ralph points the worker at the plan's 'What will change' section,
    and the worker is told to write files (not just report)."""

    def test_non_testable_worker_targets_what_will_change(self) -> None:
        content = _read_skill(RALPH_SKILL)
        assert "What will change" in content, (
            "Non-testable worker prompt must target the plan's "
            "'What will change' section."
        )

    def test_non_testable_worker_produces_diff(self) -> None:
        """The worker writes COMPLETE only when files have been edited
        (implementation — not report)."""
        content = _read_skill(RALPH_SKILL)
        # The non-testable worker writes "COMPLETE" to work-status.txt
        # after applying edits and passing applicable pre-PR checks —
        # same completion contract as the testable worker. The output
        # is a real diff, which the outer /task-v2-ralph captures via
        # `git diff --name-only HEAD`.
        assert 'write "COMPLETE" to `{worktree}/tmp/ralph/work-status.txt`' in content, (
            "Non-testable worker must write COMPLETE on success."
        )
        # Pre-PR checks list must include per-file-type gates so the
        # worker touches real files and verifies them.
        assert "ruff check" in content, (
            "Non-testable worker must run ruff on touched .py files."
        )
        assert "check-markdown-links.sh" in content, (
            "Non-testable worker must run markdown-link check on "
            "touched .md files."
        )


class TestTaxonomyConsistency:
    """The testable / non-testable enumerations must match between
    `/task-v2-ralph` (which seeds `## Testable` in task.md) and
    `/ralph` (which reads `## Testable` in task.md).

    Without this consistency, the outer skill could classify a change
    one way and the inner skill read it the other way."""

    TESTABLE = ("api", "scraper", "ingestion", "web", "backfill_script", "agent_skill")
    NON_TESTABLE = ("docs", "db_migration", "dx_tooling", "no_deployed_component")

    @pytest.mark.parametrize("ct", TESTABLE)
    def test_task_v2_ralph_lists_testable(self, ct: str) -> None:
        """Outer skill enumerates every testable type."""
        content = _read_skill(TASK_V2_RALPH_SKILL)
        assert ct in content, f"/task-v2-ralph must list testable type `{ct}`"

    @pytest.mark.parametrize("ct", NON_TESTABLE)
    def test_task_v2_ralph_lists_non_testable(self, ct: str) -> None:
        """Outer skill enumerates every non-testable type."""
        content = _read_skill(TASK_V2_RALPH_SKILL)
        assert ct in content, f"/task-v2-ralph must list non-testable type `{ct}`"

    @pytest.mark.parametrize("ct", NON_TESTABLE)
    def test_ralph_lists_non_testable(self, ct: str) -> None:
        """Inner skill enumerates every non-testable type (so the
        reviewer prompt knows the complete set)."""
        content = _read_skill(RALPH_SKILL)
        assert ct in content, f"/ralph must list non-testable type `{ct}`"
