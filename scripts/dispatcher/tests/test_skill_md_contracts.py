"""Contract tests verifying expected text patterns in skill SKILL.md files.

These tests assert that:

* ``prior_attempts.md`` is referenced in ``task-v2-ralph/SKILL.md`` Step 0.5
  and the worker-context handoff (AC b.3).
* ``## Prior attempts`` is referenced in ``ralph/SKILL.md`` worker prompt AND
  Claude-reviewer prompt (AC b.4).
* ``iteration_feedback.py`` and ``## Iteration feedback`` appear in
  ``task-v2-ralph/SKILL.md`` Step 4 (AC a.1 grep half).

These are grep-style assertions against the literal SKILL.md text — they catch
regressions where an edit removes or renames a required marker.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # worktree root

_TASK_V2_RALPH_SKILL = _REPO_ROOT / ".claude" / "skills" / "task-v2-ralph" / "SKILL.md"
_RALPH_SKILL = _REPO_ROOT / ".claude" / "skills" / "ralph" / "SKILL.md"


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
