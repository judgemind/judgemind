"""Tests for scripts/dispatcher/collapse_comments.py (issue #2716)."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.collapse_comments import collapse_comments, estimate_tokens  # noqa: E402


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_formula() -> None:
    """estimate_tokens returns int(len(text) / 3.5)."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 35) == 10
    assert estimate_tokens("a" * 350) == 100


# ---------------------------------------------------------------------------
# collapse_comments edge cases
# ---------------------------------------------------------------------------


def test_empty_list_returns_empty() -> None:
    """Empty input returns empty output."""
    result = collapse_comments([])
    assert result == []


def test_under_threshold_returns_identity() -> None:
    """A comment array whose total body tokens are under the threshold is
    returned verbatim (same list object)."""
    # 5 comments × ~350 chars each → ~500 tokens total (well under 2000).
    comments = [
        {"author": f"user{i}", "date": "2025-01-01", "body": "x" * 350}
        for i in range(5)
    ]
    result = collapse_comments(comments)
    # Must be the same object — identity, not a copy.
    assert result is comments


def test_over_threshold_collapses() -> None:
    """When total tokens exceed the threshold, middle entries are collapsed.

    10 comments × ~1750 chars each → ~5000 tokens → triggers collapse.
    After collapse:
    - output[0] is input[0] verbatim.
    - output[-2:] are input[-2:] verbatim.
    - All middle entries have _collapsed=True and body starting with '(collapsed) '.
    - Collapsed output total tokens are well under 1000.
    """
    body = "y" * 1750  # ~500 tokens each
    comments = [
        {"author": f"user{i}", "date": "2025-01-01", "body": body} for i in range(10)
    ]
    result = collapse_comments(comments)

    # First and last two are verbatim.
    assert result[0] is comments[0]
    assert result[-2] is comments[-2]
    assert result[-1] is comments[-1]

    # Middle entries (indices 1 to -3 in original → indices 1 to len-3 in result)
    middle = result[1:-2]
    assert len(middle) == 7  # 10 comments - 1 first - 2 last
    for entry in middle:
        assert entry.get("_collapsed") is True
        assert entry["body"].startswith("(collapsed) ")

    # Collapsed output total tokens are much smaller than the original.
    original_total = sum(estimate_tokens(c.get("body", "")) for c in comments)
    collapsed_total = sum(estimate_tokens(e.get("body", "")) for e in result)
    # Middle entries are capped at 120 chars ("(collapsed) " + 120 body chars),
    # so the collapsed total is dramatically smaller than the original.
    assert collapsed_total < original_total // 2


def test_short_thread_stays_verbatim() -> None:
    """≤3 entries are always returned verbatim, even if total tokens are huge."""
    body = "z" * 10_000  # ~2857 tokens each, well over threshold
    comments = [
        {"author": f"user{i}", "date": "2025-01-01", "body": body} for i in range(2)
    ]
    result = collapse_comments(comments)
    assert result is comments


def test_threshold_boundary() -> None:
    """Tokens exactly below threshold → identity; at or above → collapse."""
    # Build comments whose bodies total to exactly around the boundary.
    # threshold_tokens=2000; each char counts as 1/3.5 tokens.
    # To get 1999 tokens: total_chars = int(1999 * 3.5) = 6996 chars.
    # Spread across 4 comments (>3 so collapse logic can fire).

    chars_for_1999_tokens = int(1999 * 3.5)  # 6996
    # Distribute evenly across 4 comments.
    body_under = "a" * (chars_for_1999_tokens // 4)
    comments_under = [
        {"author": f"u{i}", "date": "2025-01-01", "body": body_under} for i in range(4)
    ]
    # Sanity: total estimated tokens should be < 2000.
    total_under = sum(estimate_tokens(c["body"]) for c in comments_under)
    assert total_under < 2000

    result_under = collapse_comments(comments_under)
    assert result_under is comments_under  # identity

    # For exactly-over: use chars_for_1999_tokens + 4 extra chars (one per comment).
    body_over = "a" * (chars_for_1999_tokens // 4 + 1)
    comments_over = [
        {"author": f"u{i}", "date": "2025-01-01", "body": body_over} for i in range(4)
    ]
    total_over = sum(estimate_tokens(c["body"]) for c in comments_over)
    assert total_over >= 2000

    result_over = collapse_comments(comments_over)
    assert result_over is not comments_over  # collapsed — different object
    # Middle entries are collapsed.
    for entry in result_over[1:-2]:
        assert entry.get("_collapsed") is True
