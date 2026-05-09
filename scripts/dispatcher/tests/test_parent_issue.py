"""Unit tests for the shared ``parse_parent_issue`` helper.

The same regex previously lived in three places (#4508). These tests
exercise the canonical helper directly; the pre-existing
``test_daemon_phase3a.py::TestParseParentIssue`` cases keep covering
the daemon's static-method shim that delegates here.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure ``scripts/dispatcher`` is importable when this test runs
# under pytest's local-default collection (the dispatcher tests dir
# already sets this up via conftest, but doing it here too makes the
# module importable in isolation).
_DISPATCHER_DIR = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_DIR))

from parent_issue import PARENT_RE, parse_parent_issue  # noqa: E402


class TestParseParentIssue:
    def test_parse_parent_issue_canonical_form(self) -> None:
        assert parse_parent_issue("Parent: #2782") == 2782

    def test_parse_parent_issue_lowercase_form(self) -> None:
        assert parse_parent_issue("parent: #100") == 100

    def test_parse_parent_issue_with_surrounding_text(self) -> None:
        body = "## Summary\n\nFix the thing.\n\nParent: #42\n\nMore text.\n"
        assert parse_parent_issue(body) == 42

    def test_parse_parent_issue_tolerates_whitespace(self) -> None:
        assert parse_parent_issue("   Parent:    #7   ") == 7

    def test_parse_parent_issue_first_match_wins(self) -> None:
        body = "Parent: #1\n\nParent: #2\n"
        assert parse_parent_issue(body) == 1

    def test_parse_parent_issue_returns_none_when_absent(self) -> None:
        assert parse_parent_issue("nope") is None

    def test_parse_parent_issue_returns_none_for_empty_string(self) -> None:
        assert parse_parent_issue("") is None

    def test_parse_parent_issue_returns_none_for_none_input(self) -> None:
        assert parse_parent_issue(None) is None

    def test_parse_parent_issue_does_not_match_inline_reference(self) -> None:
        # The regex requires the parent line to be the only content on
        # that line — narrative prose like "the parent issue #42 is..."
        # must not match.
        assert parse_parent_issue("the parent issue #42 is open") is None

    def test_parse_parent_issue_does_not_match_without_colon(self) -> None:
        # ``Parent #N`` (no colon) is intentionally out of scope per
        # the issue body's "Out of Scope" section.
        assert parse_parent_issue("Parent #42") is None

    def test_parse_parent_issue_multiline_body_no_match(self) -> None:
        body = "Line 1\nLine 2\n\nLine 3\n"
        assert parse_parent_issue(body) is None


class TestParentRe:
    def test_pattern_is_compiled(self) -> None:
        # Sanity: PARENT_RE is exposed for callers that want the
        # compiled pattern directly. The regex itself must not be
        # spelled out elsewhere — see check-no-duplicate-parent-regex.sh.
        import re as _re

        assert isinstance(PARENT_RE, _re.Pattern)

    def test_pattern_flags_are_multiline_and_ignorecase(self) -> None:
        import re as _re

        # MULTILINE and IGNORECASE must be set so anchored ``^...$``
        # matches per-line and ``Parent`` / ``parent`` both match.
        assert PARENT_RE.flags & _re.MULTILINE
        assert PARENT_RE.flags & _re.IGNORECASE
