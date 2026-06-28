"""Tests for _unblock_dependents.py strip_blocked_lines function.

Run with: python3 scripts/tests/test_unblock_dependents.py
Or with: python3 -m pytest scripts/tests/test_unblock_dependents.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Add scripts/ to path so we can import the helper
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re

from _unblock_dependents import strip_blocked_lines

# The body-match regex used in _unblock_dependents.main() — exposed here as a
# module-level constant so tests can verify the colon variant is accepted without
# invoking the full script (which requires environment variables and network access).
BLOCKED_BY_PATTERN = r"Blocked by:?\s+#(\d+)"


class TestBlockedByPatternRegex(unittest.TestCase):
    """Regression tests for the BLOCKED_BY_PATTERN constant.

    These tests exercise the body-match regex directly.  AC3 requires that the
    colon-variant test fails against the *old* pattern (``Blocked by #N`` only)
    and passes after the change.  The test below will fail on any code that does
    not include ``:?`` in the pattern.
    """

    def test_matches_canonical_no_colon(self) -> None:
        """Standard 'Blocked by #N' must match."""
        m = re.search(BLOCKED_BY_PATTERN, "Blocked by #42")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "42")

    def test_matches_colon_variant(self) -> None:
        """'Blocked by: #N' (colon variant) must match — regression for AC3."""
        m = re.search(BLOCKED_BY_PATTERN, "Blocked by: #42")
        self.assertIsNotNone(
            m, "BLOCKED_BY_PATTERN does not match the colon variant 'Blocked by: #N'"
        )
        self.assertEqual(m.group(1), "42")

    def test_extracts_all_blockers_mixed(self) -> None:
        """findall must return both canonical and colon-variant blockers."""
        body = "Blocked by #10\nBlocked by: #20\nBlocked by #30\n"
        found = re.findall(BLOCKED_BY_PATTERN, body)
        self.assertEqual(found, ["10", "20", "30"])

    def test_does_not_match_unrelated_text(self) -> None:
        """Must not match text that merely contains the word 'blocked'."""
        self.assertIsNone(re.search(BLOCKED_BY_PATTERN, "This issue is blocked."))
        self.assertIsNone(re.search(BLOCKED_BY_PATTERN, "Blocked: see comment above"))


class TestStripBlockedLines(unittest.TestCase):
    """Tests for the strip_blocked_lines body manipulation function."""

    def test_removes_single_blocked_by_line(self) -> None:
        body = "## Problem\n\nSome text\n\n## Dependencies\n\nBlocked by #42\n"
        result = strip_blocked_lines(body)
        self.assertNotIn("Blocked by #42", result)
        self.assertIn("Some text", result)

    def test_removes_multiple_blocked_by_lines(self) -> None:
        body = (
            "## Problem\n\nSome text\n\n"
            "## Dependencies\n\n"
            "Blocked by #42\nBlocked by #43\nBlocked by #44\n"
        )
        result = strip_blocked_lines(body)
        self.assertNotIn("Blocked by #42", result)
        self.assertNotIn("Blocked by #43", result)
        self.assertNotIn("Blocked by #44", result)
        self.assertIn("Some text", result)

    def test_removes_empty_dependencies_section(self) -> None:
        body = "## Problem\n\nSome text\n\n## Dependencies\n\nBlocked by #42\n"
        result = strip_blocked_lines(body)
        self.assertNotIn("## Dependencies", result)

    def test_keeps_dependencies_section_with_other_content(self) -> None:
        body = (
            "## Problem\n\nSome text\n\n"
            "## Dependencies\n\n"
            "Blocked by #42\n"
            "Parent: #10\n"
        )
        result = strip_blocked_lines(body)
        self.assertNotIn("Blocked by #42", result)
        self.assertIn("## Dependencies", result)
        self.assertIn("Parent: #10", result)

    def test_removes_trailing_blank_lines(self) -> None:
        body = "## Problem\n\nSome text\n\n## Dependencies\n\nBlocked by #42\n\n\n"
        result = strip_blocked_lines(body)
        self.assertFalse(result.endswith("\n\n"))

    def test_preserves_other_sections(self) -> None:
        body = (
            "## Problem\n\nSome problem\n\n"
            "## Acceptance Criteria\n\n- [ ] Do thing\n\n"
            "## Dependencies\n\nBlocked by #42\n"
        )
        result = strip_blocked_lines(body)
        self.assertIn("## Problem", result)
        self.assertIn("Some problem", result)
        self.assertIn("## Acceptance Criteria", result)
        self.assertIn("- [ ] Do thing", result)
        self.assertNotIn("Blocked by #42", result)
        self.assertNotIn("## Dependencies", result)

    def test_empty_body(self) -> None:
        result = strip_blocked_lines("")
        self.assertEqual(result, "")

    def test_body_with_no_blocked_by(self) -> None:
        body = "## Problem\n\nSome text\n\n## Acceptance Criteria\n\n- [ ] Do thing"
        result = strip_blocked_lines(body)
        self.assertEqual(result, body)

    def test_idempotent(self) -> None:
        body = "## Problem\n\nSome text\n\n## Dependencies\n\nBlocked by #42\n"
        first = strip_blocked_lines(body)
        second = strip_blocked_lines(first)
        self.assertEqual(first, second)

    def test_blocked_by_with_leading_whitespace(self) -> None:
        body = "## Dependencies\n\n  Blocked by #42\n"
        result = strip_blocked_lines(body)
        self.assertNotIn("Blocked by #42", result)

    def test_dependencies_before_another_heading(self) -> None:
        body = "## Dependencies\n\nBlocked by #42\n\n## Notes\n\nSome notes\n"
        result = strip_blocked_lines(body)
        self.assertNotIn("## Dependencies", result)
        self.assertIn("## Notes", result)
        self.assertIn("Some notes", result)

    def test_dependencies_section_with_mixed_content(self) -> None:
        """Dependencies section with both Blocked by lines and other content."""
        body = "## Dependencies\n\nBlocked by #42\nBlocked by #99\nRelated to #50\n"
        result = strip_blocked_lines(body)
        self.assertNotIn("Blocked by #42", result)
        self.assertNotIn("Blocked by #99", result)
        self.assertIn("Related to #50", result)
        # Section kept because it still has content
        self.assertIn("## Dependencies", result)

    # -------------------------------------------------------------------------
    # Colon-variant tests (AC2, AC3) — these FAIL against the old regex
    # -------------------------------------------------------------------------

    def test_strip_handles_colon_variant(self) -> None:
        """'Blocked by: #N' (colon variant) must be stripped — regression for AC2."""
        body = "## Problem\n\nSome text\n\n## Dependencies\n\nBlocked by: #42\n"
        result = strip_blocked_lines(body)
        self.assertNotIn("Blocked by: #42", result)
        self.assertIn("Some text", result)

    def test_strip_mixed_colon_and_no_colon(self) -> None:
        """Both canonical and colon-variant lines must be stripped in the same body."""
        body = "## Dependencies\n\nBlocked by #10\nBlocked by: #20\n"
        result = strip_blocked_lines(body)
        self.assertNotIn("Blocked by #10", result)
        self.assertNotIn("Blocked by: #20", result)
        # Section was only blockers — should be removed entirely
        self.assertNotIn("## Dependencies", result)

    def test_strip_idempotent_colon_variant(self) -> None:
        """Applying strip_blocked_lines twice on a colon-variant body yields the same output."""
        body = "## Problem\n\nSome text\n\n## Dependencies\n\nBlocked by: #42\n"
        first = strip_blocked_lines(body)
        second = strip_blocked_lines(first)
        self.assertEqual(first, second)


class TestWasBlockedByMarker(unittest.TestCase):
    """Tests for the Was-blocked-by provenance marker (issue #4610)."""

    def test_appends_marker_with_resolved_blockers(self) -> None:
        body = "## Problem\n\nSome text\n\n## Dependencies\n\nBlocked by #4282\nBlocked by #4297\n"
        result = strip_blocked_lines(
            body,
            was_blocked_by=["4282", "4297"],
            unblock_date="2026-05-08",
        )
        self.assertNotIn("Blocked by #4282", result)
        self.assertNotIn("Blocked by #4297", result)
        self.assertIn(
            "Was-blocked-by: #4282, #4297 (all closed-completed 2026-05-08; auto-unblocked)",
            result,
        )

    def test_marker_accepts_int_blockers(self) -> None:
        body = "## Dependencies\n\nBlocked by #4282\n"
        result = strip_blocked_lines(
            body, was_blocked_by=[4282], unblock_date="2026-05-08"
        )
        self.assertIn(
            "Was-blocked-by: #4282 (all closed-completed 2026-05-08; auto-unblocked)",
            result,
        )

    def test_marker_strips_hash_prefix_in_input(self) -> None:
        body = "## Dependencies\n\nBlocked by #4282\n"
        result = strip_blocked_lines(
            body, was_blocked_by=["#4282"], unblock_date="2026-05-08"
        )
        self.assertIn(
            "Was-blocked-by: #4282 (all closed-completed 2026-05-08; auto-unblocked)",
            result,
        )
        # No doubled hash.
        self.assertNotIn("##4282", result)

    def test_backward_compat_no_marker_without_args(self) -> None:
        """Calling with only body == old behavior (no marker appended)."""
        body = "## Problem\n\nSome text\n\n## Dependencies\n\nBlocked by #42\n"
        result = strip_blocked_lines(body)
        self.assertNotIn("Was-blocked-by", result)

    def test_marker_idempotent_when_called_twice(self) -> None:
        body = "## Problem\n\nSome text\n\n## Dependencies\n\nBlocked by #4282\n"
        first = strip_blocked_lines(
            body, was_blocked_by=["4282"], unblock_date="2026-05-08"
        )
        # Re-running with the same args must not duplicate the marker.
        second = strip_blocked_lines(
            first, was_blocked_by=["4282"], unblock_date="2026-05-08"
        )
        self.assertEqual(first, second)
        self.assertEqual(second.count("Was-blocked-by:"), 1)

    def test_marker_not_stripped_by_subsequent_call(self) -> None:
        """A subsequent plain strip must leave the Was-blocked-by marker in place."""
        body = "## Dependencies\n\nBlocked by #4282\n"
        first = strip_blocked_lines(
            body, was_blocked_by=["4282"], unblock_date="2026-05-08"
        )
        # Plain strip (no marker args) — must NOT remove the marker line.
        second = strip_blocked_lines(first)
        self.assertIn("Was-blocked-by: #4282", second)

    def test_empty_blocker_list_appends_nothing(self) -> None:
        body = "## Problem\n\nSome text\n"
        result = strip_blocked_lines(body, was_blocked_by=[])
        self.assertNotIn("Was-blocked-by", result)
        self.assertEqual(result, body.rstrip("\n"))

    def test_marker_uses_default_utc_date_when_unspecified(self) -> None:
        body = "## Dependencies\n\nBlocked by #4282\n"
        result = strip_blocked_lines(body, was_blocked_by=["4282"])
        self.assertRegex(
            result,
            r"Was-blocked-by: #4282 \(all closed-completed \d{4}-\d{2}-\d{2}; auto-unblocked\)",
        )


if __name__ == "__main__":
    unittest.main()
