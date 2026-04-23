"""Tests for :func:`iteration_feedback.build_iteration_feedback_section`.

Covers:

* ``test_build_section_with_real_feedback`` — multi-iteration session with real
  feedback returns a non-empty markdown section (AC a.1 unit half).
* ``test_build_section_seed_returns_empty`` — file holds only the first-iteration
  sentinel → returns empty string.
* ``test_build_section_missing_returns_empty`` — file does not exist → returns
  empty string.
* ``test_build_section_contains_feedback_verbatim`` — returned string includes the
  full feedback.md content verbatim.
* ``test_build_section_heading_format`` — returned string uses the expected
  ``## Iteration feedback (from feedback.md)`` heading.
* ``test_script_main_prints_output`` — running the module as a script prints the
  section to stdout when feedback is real.
* ``test_script_main_empty_output_on_seed`` — running as a script produces no
  output on the seed sentinel.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.iteration_feedback import (  # noqa: E402
    _SEED_SENTINEL,
    build_iteration_feedback_section,
)

_REAL_FEEDBACK = """\
## Pre-push hook failure (local gate — Step 2.5)

ruff check failed on scripts/dispatcher/daemon.py:
  Line 42: E501 line too long (102 > 100 characters)

## Reviewer feedback (iteration 2)

The `_fetch_prior_attempts` method does not guard against empty `non_infra_phases`.
Use `IN %s` with a tuple guard or fall back gracefully when the frozenset is empty.
"""


class TestBuildIterationFeedbackSection:
    def test_build_section_with_real_feedback(self, tmp_path: Path) -> None:
        """Real feedback → non-empty markdown section (AC a.1 unit half)."""
        feedback_file = tmp_path / "feedback.md"
        feedback_file.write_text(_REAL_FEEDBACK, encoding="utf-8")

        result = build_iteration_feedback_section(feedback_file)

        assert result, "Expected non-empty section for real feedback"

    def test_build_section_seed_returns_empty(self, tmp_path: Path) -> None:
        """First-iteration sentinel → empty string."""
        feedback_file = tmp_path / "feedback.md"
        feedback_file.write_text(_SEED_SENTINEL, encoding="utf-8")

        result = build_iteration_feedback_section(feedback_file)

        assert result == "", f"Expected empty string for seed sentinel, got: {result!r}"

    def test_build_section_missing_returns_empty(self, tmp_path: Path) -> None:
        """Missing file → empty string."""
        missing = tmp_path / "does_not_exist.md"

        result = build_iteration_feedback_section(missing)

        assert result == "", f"Expected empty string for missing file, got: {result!r}"

    def test_build_section_contains_feedback_verbatim(self, tmp_path: Path) -> None:
        """The section must include the feedback.md content verbatim."""
        feedback_file = tmp_path / "feedback.md"
        feedback_file.write_text(_REAL_FEEDBACK, encoding="utf-8")

        result = build_iteration_feedback_section(feedback_file)

        assert _REAL_FEEDBACK in result, (
            "Returned section must include the feedback.md content verbatim"
        )

    def test_build_section_heading_format(self, tmp_path: Path) -> None:
        """Heading must use the expected ``## Iteration feedback`` format."""
        feedback_file = tmp_path / "feedback.md"
        feedback_file.write_text(_REAL_FEEDBACK, encoding="utf-8")

        result = build_iteration_feedback_section(feedback_file)

        assert "## Iteration feedback (from feedback.md)" in result

    def test_build_section_seed_with_whitespace_returns_empty(
        self, tmp_path: Path
    ) -> None:
        """Sentinel with surrounding whitespace still returns empty."""
        feedback_file = tmp_path / "feedback.md"
        feedback_file.write_text(f"\n  {_SEED_SENTINEL}  \n", encoding="utf-8")

        result = build_iteration_feedback_section(feedback_file)

        assert result == ""

    def test_build_section_empty_file_returns_empty(self, tmp_path: Path) -> None:
        """Completely empty file returns empty string."""
        feedback_file = tmp_path / "feedback.md"
        feedback_file.write_text("", encoding="utf-8")

        result = build_iteration_feedback_section(feedback_file)

        assert result == ""

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        """Function accepts str path in addition to Path objects."""
        feedback_file = tmp_path / "feedback.md"
        feedback_file.write_text(_REAL_FEEDBACK, encoding="utf-8")

        result = build_iteration_feedback_section(str(feedback_file))

        assert result, "Expected non-empty section when path passed as str"


class TestScriptMain:
    """Test the module's ``__main__`` entry point."""

    def test_script_main_prints_output(self, tmp_path: Path) -> None:
        """Running as a script prints section to stdout for real feedback."""
        feedback_file = tmp_path / "feedback.md"
        feedback_file.write_text(_REAL_FEEDBACK, encoding="utf-8")
        script = _SCRIPTS / "dispatcher" / "iteration_feedback.py"

        proc = subprocess.run(
            [sys.executable, str(script), str(feedback_file)],
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0
        assert "## Iteration feedback" in proc.stdout
        assert _REAL_FEEDBACK in proc.stdout

    def test_script_main_empty_output_on_seed(self, tmp_path: Path) -> None:
        """Running as a script produces no stdout output for the seed sentinel."""
        feedback_file = tmp_path / "feedback.md"
        feedback_file.write_text(_SEED_SENTINEL, encoding="utf-8")
        script = _SCRIPTS / "dispatcher" / "iteration_feedback.py"

        proc = subprocess.run(
            [sys.executable, str(script), str(feedback_file)],
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_script_main_missing_arg_exits_nonzero(self) -> None:
        """Running without argument exits with non-zero code."""
        script = _SCRIPTS / "dispatcher" / "iteration_feedback.py"

        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
        )

        assert proc.returncode != 0
