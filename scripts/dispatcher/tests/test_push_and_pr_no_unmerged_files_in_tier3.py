"""Issue #3558 — verify ``FAILURE_CATEGORY_PUSH_AND_PR_NO_UNMERGED_FILES``
is a member of ``TIER_3_CATEGORIES`` in ``daemon.py``.

The operational bug fixed in #3558: ``FAILURE_CATEGORY_PUSH_AND_PR_NO_UNMERGED_FILES``
was referenced in ``BYPASSED_TERMINAL_PHASES_TO_ROUTE`` but absent from all tier
sets (TIER_2_FIRST_OCCURRENCE_CATEGORIES, TIER_2_RECURRENCE_CATEGORIES, TIER_3_CATEGORIES).
``_find_diagnoser_candidates`` (daemon.py) drops any failure whose category is not in
those tier sets, so the diagnoser never fired — the issue got re-claimed on the next
tick instead of being routed to the diagnoser.

This test guards against regression of that membership.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add the dispatcher package to sys.path so we can import daemon symbols.
_DISPATCHER_DIR = Path(__file__).resolve().parents[1]
if str(_DISPATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(_DISPATCHER_DIR))

from daemon import (  # noqa: E402
    FAILURE_CATEGORY_PUSH_AND_PR_NO_UNMERGED_FILES,
    TIER_3_CATEGORIES,
)


class TestPushAndPrNoUnmergedFilesInTier3:
    """``FAILURE_CATEGORY_PUSH_AND_PR_NO_UNMERGED_FILES`` must be in
    ``TIER_3_CATEGORIES`` so ``_find_diagnoser_candidates`` picks it up
    for the diagnoser sweep (#3558)."""

    def test_category_in_tier3(self) -> None:
        assert FAILURE_CATEGORY_PUSH_AND_PR_NO_UNMERGED_FILES in TIER_3_CATEGORIES, (
            f"FAILURE_CATEGORY_PUSH_AND_PR_NO_UNMERGED_FILES "
            f"({FAILURE_CATEGORY_PUSH_AND_PR_NO_UNMERGED_FILES!r}) must be in "
            f"TIER_3_CATEGORIES to allow _find_diagnoser_candidates to select it "
            f"(#3558 regression guard)"
        )

    def test_category_value(self) -> None:
        """The constant's string value must match the phase/category name used
        by the entrypoint and BYPASSED_TERMINAL_PHASES_TO_ROUTE."""
        assert FAILURE_CATEGORY_PUSH_AND_PR_NO_UNMERGED_FILES == "push_and_pr_no_unmerged_files", (
            "FAILURE_CATEGORY_PUSH_AND_PR_NO_UNMERGED_FILES value changed — "
            "update BYPASSED_TERMINAL_PHASES_TO_ROUTE and entrypoint accordingly"
        )
