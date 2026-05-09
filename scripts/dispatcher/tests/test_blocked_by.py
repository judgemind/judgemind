"""Unit tests for the shared ``parse_blocked_by`` helper.

The same regex previously lived in three places (#4514). These tests
exercise the canonical helper directly; the pre-existing
``test_daemon_phase3a.py::TestParseBlockedBy`` cases keep covering
the daemon's static-method shim that delegates here, and
``test_daemon_enrichment.py::TestNormaliseIssueRecord`` keeps covering
the enrichment-time call site.
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

from blocked_by import BLOCKED_BY_RE, parse_blocked_by  # noqa: E402


class TestParseBlockedBy:
    def test_parse_blocked_by_canonical_form(self) -> None:
        assert parse_blocked_by("Blocked by #2782") == [2782]

    def test_parse_blocked_by_lowercase_form(self) -> None:
        assert parse_blocked_by("blocked by #100") == [100]

    def test_parse_blocked_by_colon_variant(self) -> None:
        # ``Blocked by: #N`` is the colon-suffixed shape some humans
        # write — must be accepted alongside the canonical no-colon
        # form. Same convention scripts/unblock-dependents.sh uses.
        assert parse_blocked_by("Blocked by: #42") == [42]

    def test_parse_blocked_by_with_surrounding_text(self) -> None:
        body = "## Summary\n\nFix the thing.\n\nBlocked by #42\n\nMore text.\n"
        assert parse_blocked_by(body) == [42]

    def test_parse_blocked_by_tolerates_whitespace(self) -> None:
        assert parse_blocked_by("   Blocked by    #7   ") == [7]

    def test_parse_blocked_by_returns_all_matches_in_order(self) -> None:
        # Multiple ``Blocked by`` lines must all be returned in
        # document order — the daemon's enrichment paths consume the
        # full list.
        body = "Blocked by #1\n\nBlocked by #2\nBlocked by #3\n"
        assert parse_blocked_by(body) == [1, 2, 3]

    def test_parse_blocked_by_mixes_colon_and_no_colon(self) -> None:
        # Same body fixture that ``test_daemon_enrichment.py`` exercises.
        body = "Blocked by #10\nBlocked by: #20\n"
        assert parse_blocked_by(body) == [10, 20]

    def test_parse_blocked_by_returns_empty_when_absent(self) -> None:
        assert parse_blocked_by("nope") == []

    def test_parse_blocked_by_returns_empty_for_empty_string(self) -> None:
        assert parse_blocked_by("") == []

    def test_parse_blocked_by_returns_empty_for_none_input(self) -> None:
        assert parse_blocked_by(None) == []

    def test_parse_blocked_by_does_not_match_inline_reference(self) -> None:
        # The regex requires the blocker line to be the only content on
        # that line — narrative prose like "this is blocked by #42 and
        # #43" must not match.
        assert parse_blocked_by("this issue is blocked by #42 and others") == []

    def test_parse_blocked_by_does_not_match_without_hash(self) -> None:
        assert parse_blocked_by("Blocked by 42") == []

    def test_parse_blocked_by_multiline_body_no_match(self) -> None:
        body = "Line 1\nLine 2\n\nLine 3\n"
        assert parse_blocked_by(body) == []


class TestBlockedByRe:
    def test_pattern_is_compiled(self) -> None:
        # Sanity: BLOCKED_BY_RE is exposed for callers that want the
        # compiled pattern directly. The regex itself must not be
        # spelled out elsewhere — see check-no-duplicate-blocked-by-regex.sh.
        import re as _re

        assert isinstance(BLOCKED_BY_RE, _re.Pattern)

    def test_pattern_flags_are_multiline_and_ignorecase(self) -> None:
        import re as _re

        # MULTILINE and IGNORECASE must be set so anchored ``^...$``
        # matches per-line and ``Blocked`` / ``blocked`` both match.
        assert BLOCKED_BY_RE.flags & _re.MULTILINE
        assert BLOCKED_BY_RE.flags & _re.IGNORECASE
