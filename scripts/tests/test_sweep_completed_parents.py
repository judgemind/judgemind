"""Unit tests for ``scripts/_sweep_completed_parents.py`` (#4499).

Exercises the pure-function gating logic without invoking the GitHub CLI:

* ``parse_parent_number`` — extracts the canonical ``Parent: #N`` line and
  ignores spurious matches.
* ``group_children_by_parent`` — collapses a flat children list into a
  parent-keyed mapping.
* ``is_parent_closeable`` — the closeability gate with grace-window /
  state / state-reason short-circuits.
* ``has_human_comments_since`` — the post-close comment guard, including
  the bot-author filter for ``judgemind-agent`` and ``[bot]`` suffixes.

Run with::

    python3 -m pytest scripts/tests/test_sweep_completed_parents.py -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add scripts/ to path so we can import the helper module directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# noqa: E402 — sys.path mutation above
from _sweep_completed_parents import (
    _Child,
    group_children_by_parent,
    has_human_comments_since,
    is_parent_closeable,
    parse_parent_number,
)


class TestParseParentNumber(unittest.TestCase):
    def test_canonical_form(self) -> None:
        self.assertEqual(parse_parent_number("Parent: #4097"), 4097)

    def test_with_surrounding_text(self) -> None:
        body = (
            "## Summary\n\nDoes a thing.\n\n## Out of Scope\n\nNothing.\n\n"
            "Parent: #42\n"
        )
        self.assertEqual(parse_parent_number(body), 42)

    def test_lowercase_parent(self) -> None:
        # The regex is case-insensitive (matches the daemon's parser).
        self.assertEqual(parse_parent_number("parent: #100"), 100)

    def test_extra_whitespace(self) -> None:
        self.assertEqual(parse_parent_number("   Parent:    #7   "), 7)

    def test_inline_does_not_match(self) -> None:
        # The regex anchors to a line start (^) — inline mentions are
        # ignored to avoid false positives.
        self.assertIsNone(parse_parent_number("See Parent: #5 in another doc"))

    def test_no_parent_line(self) -> None:
        self.assertIsNone(parse_parent_number("## Summary\nNo parent here.\n"))

    def test_empty_body(self) -> None:
        self.assertIsNone(parse_parent_number(""))

    def test_takes_first_match(self) -> None:
        # Multiple Parent: lines in the body — the parser takes the first.
        body = "Parent: #1\nFiller\nParent: #2\n"
        self.assertEqual(parse_parent_number(body), 1)


class TestGroupChildrenByParent(unittest.TestCase):
    def test_groups_by_parent(self) -> None:
        children = [
            {
                "number": 4137,
                "state": "CLOSED",
                "closedAt": "2026-05-06T06:21:36Z",
                "body": "Parent: #4097\n",
            },
            {
                "number": 4138,
                "state": "CLOSED",
                "closedAt": "2026-05-06T08:00:00Z",
                "body": "Parent: #4097\n",
            },
            {
                "number": 4139,
                "state": "CLOSED",
                "closedAt": "2026-05-06T10:43:00Z",
                "body": "Parent: #4097\n",
            },
        ]
        grouped = group_children_by_parent(children)
        self.assertEqual(set(grouped.keys()), {4097})
        self.assertEqual(
            sorted(c.number for c in grouped[4097]),
            [4137, 4138, 4139],
        )

    def test_skips_children_without_parent(self) -> None:
        children = [
            {"number": 1, "state": "OPEN", "closedAt": None, "body": "no parent"},
            {
                "number": 2,
                "state": "CLOSED",
                "closedAt": "2026-05-01T00:00:00Z",
                "body": "Parent: #99\n",
            },
        ]
        grouped = group_children_by_parent(children)
        self.assertEqual(set(grouped.keys()), {99})
        self.assertEqual([c.number for c in grouped[99]], [2])

    def test_multiple_parents(self) -> None:
        children = [
            {
                "number": 1,
                "state": "CLOSED",
                "closedAt": "2026-05-01T00:00:00Z",
                "body": "Parent: #10\n",
            },
            {
                "number": 2,
                "state": "CLOSED",
                "closedAt": "2026-05-02T00:00:00Z",
                "body": "Parent: #20\n",
            },
        ]
        grouped = group_children_by_parent(children)
        self.assertEqual(set(grouped.keys()), {10, 20})


class TestIsParentCloseable(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
        self.grace = timedelta(hours=24)

    def _child(
        self,
        n: int,
        state: str = "CLOSED",
        closed_at: datetime | None = None,
        state_reason: str | None = None,
    ) -> _Child:
        return _Child(
            number=n,
            state=state,
            closed_at=closed_at,
            state_reason=state_reason,
        )

    def test_all_children_closed_outside_grace(self) -> None:
        # Most-recent child closed 36h before now — outside the 24h grace.
        children = [
            self._child(1, closed_at=self.now - timedelta(hours=72)),
            self._child(2, closed_at=self.now - timedelta(hours=36)),
            self._child(3, closed_at=self.now - timedelta(hours=48)),
        ]
        ok, reason = is_parent_closeable(
            "OPEN", children, now=self.now, grace=self.grace
        )
        self.assertTrue(ok, msg=f"unexpected skip reason: {reason}")

    def test_inside_grace_window_is_skipped(self) -> None:
        # Most-recent child closed only 6h ago — inside the 24h grace.
        children = [
            self._child(1, closed_at=self.now - timedelta(hours=72)),
            self._child(2, closed_at=self.now - timedelta(hours=6)),
        ]
        ok, reason = is_parent_closeable(
            "OPEN", children, now=self.now, grace=self.grace
        )
        self.assertFalse(ok)
        self.assertIn("grace", reason)

    def test_open_child_blocks_close(self) -> None:
        children = [
            self._child(1, closed_at=self.now - timedelta(hours=72)),
            self._child(2, state="OPEN", closed_at=None),
        ]
        ok, reason = is_parent_closeable(
            "OPEN", children, now=self.now, grace=self.grace
        )
        self.assertFalse(ok)
        self.assertIn("open children", reason)

    def test_not_planned_child_blocks_close(self) -> None:
        children = [
            self._child(1, closed_at=self.now - timedelta(hours=72)),
            self._child(
                2,
                closed_at=self.now - timedelta(hours=72),
                state_reason="not_planned",
            ),
        ]
        ok, reason = is_parent_closeable(
            "OPEN", children, now=self.now, grace=self.grace
        )
        self.assertFalse(ok)
        self.assertIn("not-planned", reason)

    def test_closed_parent_is_skipped(self) -> None:
        children = [
            self._child(1, closed_at=self.now - timedelta(hours=72)),
        ]
        ok, reason = is_parent_closeable(
            "CLOSED", children, now=self.now, grace=self.grace
        )
        self.assertFalse(ok)
        self.assertIn("CLOSED", reason)

    def test_no_children_is_skipped(self) -> None:
        ok, reason = is_parent_closeable("OPEN", [], now=self.now, grace=self.grace)
        self.assertFalse(ok)
        self.assertIn("no children", reason)

    def test_no_closed_at_timestamps_is_skipped(self) -> None:
        children = [self._child(1, state="CLOSED", closed_at=None)]
        ok, reason = is_parent_closeable(
            "OPEN", children, now=self.now, grace=self.grace
        )
        self.assertFalse(ok)
        self.assertIn("closedAt", reason)

    def test_completed_state_reason_does_not_block(self) -> None:
        # ``state_reason="completed"`` is fine — only ``not_planned`` blocks.
        children = [
            self._child(
                1,
                closed_at=self.now - timedelta(hours=72),
                state_reason="completed",
            ),
        ]
        ok, _reason = is_parent_closeable(
            "OPEN", children, now=self.now, grace=self.grace
        )
        self.assertTrue(ok)


class TestHasHumanCommentsSince(unittest.TestCase):
    def setUp(self) -> None:
        self.cutoff = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)

    def _comment(
        self,
        login: str,
        created: str,
        cid: str = "id",
    ) -> dict:
        return {
            "id": cid,
            "createdAt": created,
            "author": {"login": login},
        }

    def test_human_comment_after_cutoff_returns_true(self) -> None:
        comments = [
            self._comment("drewthaler", "2026-05-07T00:00:00Z", "h1"),
        ]
        has_human, ids = has_human_comments_since(comments, self.cutoff)
        self.assertTrue(has_human)
        self.assertEqual(ids, ["h1"])

    def test_bot_comment_after_cutoff_returns_false(self) -> None:
        # judgemind-agent is in the explicit allowlist.
        comments = [
            self._comment("judgemind-agent", "2026-05-07T00:00:00Z", "b1"),
        ]
        has_human, _ids = has_human_comments_since(comments, self.cutoff)
        self.assertFalse(has_human)

    def test_github_actions_bot_is_filtered(self) -> None:
        comments = [
            self._comment("github-actions[bot]", "2026-05-07T00:00:00Z"),
        ]
        has_human, _ids = has_human_comments_since(comments, self.cutoff)
        self.assertFalse(has_human)

    def test_arbitrary_bot_suffix_is_filtered(self) -> None:
        comments = [
            self._comment("dependabot[bot]", "2026-05-07T00:00:00Z"),
        ]
        has_human, _ids = has_human_comments_since(comments, self.cutoff)
        self.assertFalse(has_human)

    def test_comments_at_or_before_cutoff_are_ignored(self) -> None:
        # Strict > (greater-than), not >=. Equal timestamps don't count.
        comments = [
            self._comment("drewthaler", "2026-05-06T10:00:00Z"),
            self._comment("drewthaler", "2026-05-06T09:59:00Z"),
        ]
        has_human, ids = has_human_comments_since(comments, self.cutoff)
        self.assertFalse(has_human)
        self.assertEqual(ids, [])

    def test_mixed_human_and_bot_returns_true(self) -> None:
        comments = [
            self._comment("github-actions[bot]", "2026-05-07T00:00:00Z"),
            self._comment("drewthaler", "2026-05-07T01:00:00Z"),
        ]
        has_human, _ids = has_human_comments_since(comments, self.cutoff)
        self.assertTrue(has_human)


if __name__ == "__main__":
    unittest.main()
