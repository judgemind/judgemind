"""Smoke test: the Step 5.5 snippet in task-v2-plan/SKILL.md runs cleanly.

Extracts the first ```python``` block under ``## Step 5.5``, prepends a
runnable preamble (``issue_comments = []``), and subprocess-runs it from the
repo root.  Asserts exit 0.

This test catches future SKILL edits that re-break the import path — it uses
the real package layout with no extra PYTHONPATH priming so it precisely
simulates an agent invoking the snippet from the worktree root.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # scripts/dispatcher/tests → repo root
_SKILL_PATH = _REPO_ROOT / ".claude" / "skills" / "task-v2-plan" / "SKILL.md"


def _extract_step_55_snippet(skill_text: str) -> str:
    """Return the first ```python``` block under the ``## Step 5.5`` heading."""
    # Find the Step 5.5 section.
    step_55_match = re.search(r"^## Step 5\.5\b", skill_text, re.MULTILINE)
    assert step_55_match is not None, (
        "task-v2-plan/SKILL.md must contain a '## Step 5.5' section"
    )
    section_text = skill_text[step_55_match.start() :]

    # Find the first ```python ... ``` fenced block within that section.
    code_match = re.search(r"```python\n(.*?)```", section_text, re.DOTALL)
    assert code_match is not None, (
        "task-v2-plan/SKILL.md ## Step 5.5 must contain a ```python``` code block"
    )
    return code_match.group(1)


def test_skill_step_55_snippet_runs() -> None:
    """The Step 5.5 snippet imports and runs without error from the repo root."""
    assert _SKILL_PATH.exists(), f"SKILL.md not found: {_SKILL_PATH}"

    skill_text = _SKILL_PATH.read_text(encoding="utf-8")
    snippet = _extract_step_55_snippet(skill_text)

    # Prepend a minimal preamble so the snippet is a complete program.
    program = "issue_comments = []\n" + snippet

    # Run with a clean environment: no PYTHONPATH priming so we precisely
    # simulate an agent invoking the snippet from the worktree root.
    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        env=clean_env,
    )

    assert result.returncode == 0, (
        f"Step 5.5 snippet exited with code {result.returncode}.\n"
        f"--- stderr ---\n{result.stderr}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- program ---\n{program}"
    )
