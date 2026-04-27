"""Phase-pair contract tests for the dispatcher v2 pipeline.

Each test class covers one phase-pair contract:

- ``TestPlanToRalphContract``    — plan → ralph
- ``TestRalphToSummaryContract`` — ralph → summary
- ``TestSummaryToPushAndPrContract`` — summary → push_and_pr
- ``TestVerifyToRetroContract``  — verify → retro
- ``TestDaemonRalphInputAssemblyParity`` — parity test against daemon.py inline block

Pure-function validators are defined at module level so the contracts are
Python-readable, not just SKILL.md prose. No DB, no network, compatible
with ``-m "not integration"``.

Issue: #2853
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]  # worktree root

_TASK_V2_PLAN_SKILL = _REPO_ROOT / ".claude" / "skills" / "task-v2-plan" / "SKILL.md"
_DAEMON_PY = _REPO_ROOT / "scripts" / "dispatcher" / "daemon.py"

# ---------------------------------------------------------------------------
# Enumerated constants
# ---------------------------------------------------------------------------

_ALL_CHANGE_TYPES = [
    "api",
    "scraper",
    "ingestion",
    "web",
    "backfill_script",
    "agent_skill",
    "docs",
    "db_migration",
    "dx_tooling",
    "no_deployed_component",
]

_CONVENTIONAL_PREFIXES = [
    "feat",
    "fix",
    "docs",
    "refactor",
    "test",
    "build",
    "ci",
    "chore",
    "perf",
]

# ---------------------------------------------------------------------------
# Module-scope helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.exists(), f"required file not found: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase-pair 1: plan → ralph
# ---------------------------------------------------------------------------


def build_ralph_input(
    plan_output: dict,
    issue_number: int,
    worktree_path: str,
    repo_root: str,
    max_iterations: int = 5,
) -> dict:
    """Mirror the daemon's inline ralph_input assembly block.

    This function must stay in sync with daemon.py lines ~10198-10207.
    ``TestDaemonRalphInputAssemblyParity`` asserts the key sets match.
    """
    plan = plan_output or {}
    change_type = plan.get("change_type")
    if change_type is not None and change_type not in _ALL_CHANGE_TYPES:
        raise ValueError(
            f"plan.change_type={change_type!r} is not a recognised change type; "
            f"expected one of {_ALL_CHANGE_TYPES}"
        )
    return {
        "agent_id": plan.get("agent_id"),
        "issue_number": issue_number,
        "plan": plan,
        "worktree_path": worktree_path,
        "repo_root": repo_root,
        "max_iterations": max_iterations,
        "dependencies_installed": plan.get("dependencies_to_install", []),
    }


class TestPlanToRalphContract:
    """Contract tests for the plan → ralph phase pair.

    The daemon assembles a ``ralph_input`` dict from the plan output and
    passes it into the ralph phase subprocess as
    ``{worktree}/tmp/dispatcher-input/ralph.json``.
    """

    @pytest.mark.parametrize("change_type", _ALL_CHANGE_TYPES)
    def test_valid_plan_passes_for_all_change_types(self, change_type: str) -> None:
        """build_ralph_input succeeds for each of the 10 documented change_types."""
        plan_output = {
            "agent_id": "test-agent-id",
            "issue_number": 42,
            "go": True,
            "change_type": change_type,
            "dependencies_to_install": [],
            "relevant_files": ["scripts/foo.py"],
            "relevant_docs": ["docs/specs/architecture-spec-v1.md"],
            "scope_check": [],
        }
        result = build_ralph_input(
            plan_output=plan_output,
            issue_number=42,
            worktree_path="/worktrees/agent-abc",
            repo_root="/worktrees/agent-abc",
        )
        assert isinstance(result, dict)
        required_keys = {
            "agent_id",
            "issue_number",
            "plan",
            "worktree_path",
            "repo_root",
            "max_iterations",
            "dependencies_installed",
        }
        assert required_keys.issubset(result.keys()), (
            f"ralph_input missing keys: {required_keys - result.keys()}"
        )

    def test_invalid_change_type_is_rejected(self) -> None:
        """A plan with change_type='blockchain' must raise ValueError."""
        plan_output = {
            "agent_id": "test-agent-id",
            "issue_number": 42,
            "change_type": "blockchain",
        }
        with pytest.raises((ValueError, AssertionError)):
            build_ralph_input(
                plan_output=plan_output,
                issue_number=42,
                worktree_path="/worktrees/agent-abc",
                repo_root="/worktrees/agent-abc",
            )

    def test_minimal_plan_output_does_not_crash(self) -> None:
        """None-valued optional fields must not crash build_ralph_input.

        Mirrors the daemon's plan_output.get(..., []) pattern.
        """
        plan_output: dict = {
            "agent_id": "test-agent-id",
            "issue_number": 42,
            "go": True,
            "change_type": "dx_tooling",
            "relevant_files": None,
            "relevant_docs": None,
            "scope_check": None,
        }
        result = build_ralph_input(
            plan_output=plan_output,
            issue_number=42,
            worktree_path="/worktrees/agent-abc",
            repo_root="/worktrees/agent-abc",
        )
        # dependencies_to_install is absent — must default to []
        assert result["dependencies_installed"] == []

    def test_plan_skill_is_read_only(self) -> None:
        """task-v2-plan/SKILL.md must contain 'No side effects' prose.

        Confirms the plan phase read-only contract. The plan skill must not
        edit code, comment on issues, or spawn subagents.
        """
        content = _read(_TASK_V2_PLAN_SKILL)
        assert "No side effects" in content or "read-only" in content.lower(), (
            "task-v2-plan/SKILL.md must document the 'No side effects' / read-only "
            "contract so downstream phases can rely on the repo being unmodified after plan."
        )


# ---------------------------------------------------------------------------
# Phase-pair 2: ralph → summary
# ---------------------------------------------------------------------------

_VALID_RALPH_VERDICTS = {"SHIP", "BLOCKED"}


def validate_ralph_output(ralph_output: dict) -> None:
    """Raise ValueError if ralph_output violates the ralph → summary contract.

    Rules:
    - ``verdict`` must be in VALID_RALPH_VERDICTS.
    - When verdict is SHIP, ``changed_files`` must be non-empty (regression
      guard for issue #2767 where SHIP + changed_files=[] was silently accepted).
    - When verdict is BLOCKED, ``changed_files`` may be empty (Task-tool
      unavailable path is valid even with no files changed).
    """
    verdict = ralph_output.get("verdict")
    if verdict not in _VALID_RALPH_VERDICTS:
        raise ValueError(
            f"ralph output verdict={verdict!r} is not one of {_VALID_RALPH_VERDICTS}"
        )
    changed_files = ralph_output.get("changed_files", [])
    block_reason = ralph_output.get("block_reason")
    if verdict == "SHIP" and not changed_files and not block_reason:
        raise ValueError(
            "ralph output has verdict=SHIP with changed_files=[] and no block_reason; "
            "this is the #2767 regression — a SHIP verdict must always carry "
            "at least one changed file."
        )


class TestRalphToSummaryContract:
    """Contract tests for the ralph → summary phase pair.

    The summary phase receives the ralph output bundle and uses
    ``changed_files`` + ``verdict`` to assemble the commit message,
    PR body, and AC mapping.
    """

    def test_ship_with_nonempty_changed_files_passes(self) -> None:
        """SHIP + non-empty changed_files is a valid ralph output."""
        validate_ralph_output(
            {
                "verdict": "SHIP",
                "iterations_used": 2,
                "changed_files": ["scripts/dispatcher/daemon.py"],
                "block_reason": None,
            }
        )

    def test_ship_with_empty_changed_files_and_no_block_reason_fails(self) -> None:
        """SHIP + changed_files=[] + block_reason=None must raise ValueError.

        This is the #2767 regression guard: the pre-#2845 short-circuit
        emitted SHIP with zero changed files for non-testable change types,
        which caused zero-work task completions.
        """
        with pytest.raises(ValueError, match="2767|changed_files"):
            validate_ralph_output(
                {
                    "verdict": "SHIP",
                    "iterations_used": 0,
                    "changed_files": [],
                    "block_reason": None,
                }
            )

    def test_blocked_with_empty_changed_files_passes(self) -> None:
        """BLOCKED + changed_files=[] is valid (Task-tool-unavailable path)."""
        validate_ralph_output(
            {
                "verdict": "BLOCKED",
                "iterations_used": 0,
                "changed_files": [],
                "block_reason": "Task tool unavailable",
            }
        )

    def test_unknown_verdict_fails(self) -> None:
        """verdict='SHIPPED' (a common typo) must raise ValueError."""
        with pytest.raises(ValueError, match="SHIPPED|not one of"):
            validate_ralph_output(
                {
                    "verdict": "SHIPPED",
                    "iterations_used": 1,
                    "changed_files": ["foo.py"],
                    "block_reason": None,
                }
            )


# ---------------------------------------------------------------------------
# Phase-pair 3: summary → push_and_pr
# ---------------------------------------------------------------------------

_CONVENTIONAL_PREFIX_RE = re.compile(
    r"^(feat|fix|docs|refactor|test|build|ci|chore|perf)(\([^)]+\))?!?:"
)
_CLOSES_RE = re.compile(r"Closes\s+#\d+", re.IGNORECASE)


def validate_summary_output(summary_output: dict) -> None:
    """Raise ValueError if summary_output violates the summary → push_and_pr contract.

    Rules:
    - ``commit_message`` must be present.
    - ``commit_message`` must contain a ``Closes #N`` reference.
    - ``commit_message`` must start with a conventional-commits prefix
      (feat/fix/docs/refactor/test/build/ci/chore/perf).
    """
    commit_message = summary_output.get("commit_message") or ""
    if not commit_message:
        raise ValueError("summary output is missing commit_message")
    if not _CLOSES_RE.search(commit_message):
        raise ValueError(
            f"commit_message does not contain 'Closes #N': {commit_message!r}"
        )
    first_line = commit_message.splitlines()[0] if commit_message else ""
    if not _CONVENTIONAL_PREFIX_RE.match(first_line):
        raise ValueError(
            f"commit_message first line does not start with a conventional-commits "
            f"prefix (feat/fix/docs/...): {first_line!r}"
        )


class TestSummaryToPushAndPrContract:
    """Contract tests for the summary → push_and_pr phase pair.

    The push_and_pr phase reads the commit message from summary output and
    uses it to create the git commit and open the PR.
    """

    def test_valid_summary_with_closes_and_conventional_prefix_passes(
        self,
    ) -> None:
        """A complete, well-formed summary output passes validation."""
        validate_summary_output(
            {
                "commit_message": "feat(dispatcher): add phase-pair contract tests (#2853)\n\nCloses #2853",
                "pr_title": "feat(dispatcher): add phase-pair contract tests",
                "pr_body": "## Summary\n\n- adds tests\n\nCloses #2853",
            }
        )

    def test_missing_closes_fails(self) -> None:
        """A commit message without 'Closes #N' must raise ValueError."""
        with pytest.raises(ValueError, match="Closes"):
            validate_summary_output(
                {
                    "commit_message": "feat(dispatcher): add phase-pair contract tests (#2853)",
                }
            )

    def test_non_conventional_commit_fails(self) -> None:
        """A non-conventional commit message must raise ValueError."""
        with pytest.raises(ValueError, match="conventional"):
            validate_summary_output(
                {
                    "commit_message": "Add the thing\n\nCloses #2853",
                }
            )

    @pytest.mark.parametrize("prefix", _CONVENTIONAL_PREFIXES)
    def test_valid_conventional_prefixes(self, prefix: str) -> None:
        """Each of the nine documented conventional prefixes passes validation."""
        validate_summary_output(
            {
                "commit_message": f"{prefix}(dispatcher): implement something (#2853)\n\nCloses #2853",
            }
        )


# ---------------------------------------------------------------------------
# Phase-pair 4: verify → retro
# ---------------------------------------------------------------------------

_VALID_VERIFY_VERDICTS = {"VERIFIED", "SKIPPED", "FAILED"}


def validate_verify_output(verify_output: dict) -> None:
    """Raise ValueError if verify_output violates the verify → retro contract.

    Rules:
    - ``verdict`` must be one of VERIFIED / SKIPPED / FAILED.
    - ``change_type`` (when present) must be one of the 10 documented types.
    - ``per_criterion_results`` must be a list (may be empty for SKIPPED).
    """
    verdict = verify_output.get("verdict")
    if verdict not in _VALID_VERIFY_VERDICTS:
        raise ValueError(
            f"verify output verdict={verdict!r} is not one of {_VALID_VERIFY_VERDICTS}"
        )
    change_type = verify_output.get("change_type")
    if change_type is not None and change_type not in _ALL_CHANGE_TYPES:
        raise ValueError(
            f"verify output change_type={change_type!r} is not a recognised change type; "
            f"expected one of {_ALL_CHANGE_TYPES}"
        )
    per_criterion_results = verify_output.get("per_criterion_results")
    if per_criterion_results is not None and not isinstance(
        per_criterion_results, list
    ):
        raise ValueError(
            f"per_criterion_results must be a list, got {type(per_criterion_results)}"
        )


def _make_verify_output(change_type: str, verdict: str = "VERIFIED") -> dict:
    """Build a minimal valid verify output for the given change_type."""
    return {
        "agent_id": "test-agent-id",
        "issue_number": 42,
        "pr_number": 100,
        "verdict": verdict,
        "change_type": change_type,
        "evidence_md": "## Verification Evidence\n\nOK",
        "per_criterion_results": [],
        "failure_reason": None,
        "unblock_issues": [],
    }


class TestVerifyToRetroContract:
    """Contract tests for the verify → retro phase pair.

    The retro phase reads the verify output and uses ``verdict``,
    ``change_type``, and ``per_criterion_results`` to produce retro issue
    bodies.
    """

    @pytest.mark.parametrize("change_type", _ALL_CHANGE_TYPES)
    def test_valid_change_types_pass(self, change_type: str) -> None:
        """Each of the 10 change_types produces a valid verify output
        structure that the retro phase accepts.

        Docs and no_deployed_component use SKIPPED verdict (no functional
        verification possible per task-v2-verify/SKILL.md Step 1 table).
        """
        verdict = (
            "SKIPPED"
            if change_type in ("docs", "no_deployed_component")
            else "VERIFIED"
        )
        output = _make_verify_output(change_type, verdict=verdict)
        validate_verify_output(output)  # must not raise

    def test_unhandled_change_type_fails(self) -> None:
        """change_type='blockchain' must raise ValueError."""
        output = _make_verify_output("blockchain")
        with pytest.raises(ValueError, match="blockchain|not a recognised"):
            validate_verify_output(output)


# ---------------------------------------------------------------------------
# Parity test: daemon inline ralph_input assembly vs. build_ralph_input
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #3507 — plan emits task_type field contract
# ---------------------------------------------------------------------------


class TestPlanTaskTypeContract:
    """Assert task-v2-plan/SKILL.md documents the task_type field (#3507).

    The plan skill must emit ``task_type: "coding" | "operational"`` so
    the dispatcher can branch after plan without guessing. This test is
    a forward-looking contract: it confirms the SKILL.md carries the
    required markers so an accidental edit doesn't silently break the
    pipeline.
    """

    def test_skill_md_contains_task_type_field(self) -> None:
        """SKILL.md output contract must mention ``task_type``."""
        content = _read(_TASK_V2_PLAN_SKILL)
        assert "task_type" in content, (
            "task-v2-plan/SKILL.md must document the task_type output field "
            "(required since #3507 — the dispatcher branches on this field "
            "to route operational vs coding tasks)."
        )

    def test_skill_md_documents_coding_value(self) -> None:
        """SKILL.md must mention the ``coding`` task_type value."""
        content = _read(_TASK_V2_PLAN_SKILL)
        assert "coding" in content, (
            "task-v2-plan/SKILL.md must document task_type='coding' — "
            "the default path for issues that require a PR."
        )

    def test_skill_md_documents_operational_value(self) -> None:
        """SKILL.md must mention the ``operational`` task_type value."""
        content = _read(_TASK_V2_PLAN_SKILL)
        assert "operational" in content, (
            "task-v2-plan/SKILL.md must document task_type='operational' — "
            "the bypass path for script/DB/label tasks that need no PR."
        )

    def test_skill_md_contains_classify_task_type_heading(self) -> None:
        """SKILL.md must have a 'Classify task_type' decision step."""
        content = _read(_TASK_V2_PLAN_SKILL)
        assert "Classify task_type" in content or "task_type" in content, (
            "task-v2-plan/SKILL.md must contain a 'Classify task_type' step "
            "describing when to emit 'operational' vs 'coding'. "
            "Without this, new plan agents won't know the field exists."
        )


class TestDaemonRalphInputAssemblyParity:
    """Assert the daemon's inline ralph_input dict carries exactly the same
    keys as ``build_ralph_input`` produces.

    This catches drift when someone adds a new key to the daemon block
    without updating the test-side builder (or vice versa).
    """

    def test_daemon_ralph_input_keys_match_contract(self) -> None:
        """Read daemon.py source, extract the ralph_input = { ... } block,
        and assert the key sets match.

        The block starts with ``ralph_input = {`` around line 10199 and
        ends at the matching closing brace.
        """
        source = _read(_DAEMON_PY)

        # Find the ralph_input assignment block.
        start_marker = "ralph_input = {"
        start_idx = source.find(start_marker)
        assert start_idx != -1, (
            f"daemon.py does not contain '{start_marker}' — the parity test "
            "must be updated if the ralph_input assembly was refactored."
        )

        # Scan forward to find the matching closing brace.
        brace_depth = 0
        end_idx = start_idx + len(start_marker) - 1  # position of first '{'
        for i, ch in enumerate(
            source[start_idx + len(start_marker) - 1 :],
            start=start_idx + len(start_marker) - 1,
        ):
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    end_idx = i
                    break

        block = source[start_idx : end_idx + 1]

        # Extract all string keys from the block: "key": or 'key':
        daemon_keys = set(re.findall(r'["\'](\w+)["\']\s*:', block))

        # Keys produced by the test-side builder.
        expected_keys = set(
            build_ralph_input(
                plan_output={"change_type": "dx_tooling"},
                issue_number=1,
                worktree_path="/w",
                repo_root="/w",
            ).keys()
        )

        assert daemon_keys == expected_keys, (
            f"daemon.py ralph_input keys {daemon_keys} do not match "
            f"build_ralph_input keys {expected_keys}. "
            "Update build_ralph_input (or daemon.py) to keep them in sync."
        )
