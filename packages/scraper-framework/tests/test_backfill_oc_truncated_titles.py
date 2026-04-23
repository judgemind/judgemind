"""Tests for the OC truncated title backfill script (#1360).

Tests the title recovery logic that fixes case titles truncated at the 'vs'
separator (e.g. "Saetern vs" -> "Saetern vs. Wearmouth").
"""

from __future__ import annotations

import os
import sys

# Add scripts/ to sys.path so we can import the backfill module.
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
sys.path.insert(0, os.path.normpath(_SCRIPTS_DIR))

from backfill_oc_truncated_titles import recover_title  # noqa: E402


class TestRecoverTitleEndingInVs:
    """Cases where the title ends in 'vs' or 'vs.' — missing defendant."""

    def test_simple_recovery(self) -> None:
        """Plaintiff name in title matches ruling text -> recover defendant."""
        title = recover_title(
            "Saetern vs",
            "Plaintiff Chiew Saetern vs. Wearmouth moves for an order...",
        )
        assert title is not None
        assert "Saetern" in title
        assert "Wearmouth" in title

    def test_with_trailing_dot(self) -> None:
        """Title ending in 'vs.' is also handled."""
        title = recover_title(
            "Duncan vs.",
            "Plaintiffs Kathleen Duncan vs. Hoskins petition to confirm...",
        )
        assert title is not None
        assert "Duncan" in title
        assert "Hoskins" in title

    def test_company_name(self) -> None:
        """Company names with commas are recovered."""
        title = recover_title(
            "Coulter vs.",
            "Plaintiff Coulter vs. General Motors, LLC filed a motion...",
        )
        assert title is not None
        assert "Coulter" in title
        assert "General Motors" in title

    def test_no_match_returns_none(self) -> None:
        """If plaintiff fragment doesn't match ruling text, return None."""
        title = recover_title(
            "Saetern vs",
            "This case involves a dispute between two companies...",
        )
        assert title is None


class TestRecoverTitleStartingWithVs:
    """Cases where the title starts with 'vs' or 'vs.' — missing plaintiff."""

    def test_simple_recovery(self) -> None:
        """Defendant name in title matches ruling text -> recover plaintiff."""
        title = recover_title(
            "vs. Logan",
            "Plaintiff Martinez vs. Logan alleges breach of contract...",
        )
        assert title is not None
        assert "Martinez" in title
        assert "Logan" in title

    def test_with_vs_no_dot(self) -> None:
        """Title starting with 'vs ' (no dot) is also handled."""
        title = recover_title(
            "vs Hoffman",
            "Audrey Reyes-Schiller vs. Hoffman filed this action...",
        )
        assert title is not None
        assert "Hoffman" in title

    def test_no_match_returns_none(self) -> None:
        """If defendant fragment doesn't match ruling text, return None."""
        title = recover_title(
            "vs. Logan",
            "This is unrelated ruling text with no party names...",
        )
        assert title is None


class TestRecoverTitleEdgeCases:
    """Edge cases for title recovery."""

    def test_empty_ruling_text(self) -> None:
        assert recover_title("Saetern vs", "") is None

    def test_none_ruling_text(self) -> None:
        assert recover_title("Saetern vs", None) is None  # type: ignore[arg-type]

    def test_match_is_case_insensitive(self) -> None:
        """Recovery works even if casing differs."""
        title = recover_title(
            "SAETERN vs",
            "Plaintiff Chiew Saetern vs. Wearmouth moves for...",
        )
        assert title is not None
        assert "Wearmouth" in title

    def test_normal_title_within_length_limit(self) -> None:
        """Recovered titles within 200 chars are accepted."""
        title = recover_title(
            "Smith vs",
            "Smith vs. Jones filed a motion...",
        )
        assert title is not None
        assert len(title) <= 200
