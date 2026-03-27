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

from _unblock_dependents import strip_blocked_lines


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
        body = (
            "## Dependencies\n\nBlocked by #42\n\n"
            "## Notes\n\nSome notes\n"
        )
        result = strip_blocked_lines(body)
        self.assertNotIn("## Dependencies", result)
        self.assertIn("## Notes", result)
        self.assertIn("Some notes", result)

    def test_dependencies_section_with_mixed_content(self) -> None:
        """Dependencies section with both Blocked by lines and other content."""
        body = (
            "## Dependencies\n\n"
            "Blocked by #42\n"
            "Blocked by #99\n"
            "Related to #50\n"
        )
        result = strip_blocked_lines(body)
        self.assertNotIn("Blocked by #42", result)
        self.assertNotIn("Blocked by #99", result)
        self.assertIn("Related to #50", result)
        # Section kept because it still has content
        self.assertIn("## Dependencies", result)


if __name__ == "__main__":
    unittest.main()
