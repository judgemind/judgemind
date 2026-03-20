"""Tests for the merge_near_duplicate_judges script.

Tests the name comparison utilities and near-duplicate detection logic
without requiring a live database.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from unittest.mock import MagicMock

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
merge_near = importlib.import_module("merge_near_duplicate_judges")


# ---------------------------------------------------------------------------
# Name utility tests
# ---------------------------------------------------------------------------


class TestStripAccents:
    """Tests for _strip_accents()."""

    def test_plain_ascii(self) -> None:
        assert merge_near._strip_accents("Martinez") == "Martinez"

    def test_accented_char(self) -> None:
        assert merge_near._strip_accents("Mart\u00ednez") == "Martinez"

    def test_multiple_accents(self) -> None:
        assert merge_near._strip_accents("Ren\u00e9 Mu\u00f1oz") == "Rene Munoz"


class TestLastName:
    """Tests for _last_name()."""

    def test_simple(self) -> None:
        assert merge_near._last_name("John A. Smith") == "smith"

    def test_with_suffix(self) -> None:
        assert merge_near._last_name("Robert B. Broadbelt III") == "broadbelt"

    def test_single_word(self) -> None:
        assert merge_near._last_name("Crowfoot") == "crowfoot"

    def test_with_jr(self) -> None:
        assert merge_near._last_name("Edward B. Moreton Jr.") == "moreton"


class TestFirstMiddle:
    """Tests for _first_middle()."""

    def test_simple(self) -> None:
        assert merge_near._first_middle("John A. Smith") == ["john", "a."]

    def test_with_suffix(self) -> None:
        assert merge_near._first_middle("Robert B. Broadbelt III") == ["robert", "b."]

    def test_single_word(self) -> None:
        assert merge_near._first_middle("Crowfoot") == []

    def test_first_only(self) -> None:
        assert merge_near._first_middle("Carmen Luege") == ["carmen"]


class TestIsInitial:
    """Tests for _is_initial()."""

    def test_single_letter_with_period(self) -> None:
        assert merge_near._is_initial("D.") is True

    def test_single_letter(self) -> None:
        assert merge_near._is_initial("D") is True

    def test_multi_letter(self) -> None:
        assert merge_near._is_initial("Don") is False

    def test_empty(self) -> None:
        assert merge_near._is_initial("") is False


# ---------------------------------------------------------------------------
# Near-duplicate detection tests
# ---------------------------------------------------------------------------


class TestIsNearDuplicate:
    """Tests for is_near_duplicate()."""

    def test_exact_match(self) -> None:
        assert merge_near.is_near_duplicate("John Smith", "John Smith") is True

    def test_accent_variant(self) -> None:
        """Lindsey E. Martinez vs Lindsey E. Martinez (accented i)."""
        assert (
            merge_near.is_near_duplicate("Lindsey E. Martinez", "Lindsey E. Mart\u00ednez") is True
        )

    def test_missing_middle_initial(self) -> None:
        """Carmen Luege vs Carmen R. Luege."""
        assert merge_near.is_near_duplicate("Carmen Luege", "Carmen R. Luege") is True

    def test_last_name_only_vs_full(self) -> None:
        """Crowfoot vs William A. Crowfoot."""
        assert merge_near.is_near_duplicate("Crowfoot", "William A. Crowfoot") is True

    def test_last_name_only_vs_full_2(self) -> None:
        """Mkrtchyan vs Karine Mkrtchyan."""
        assert merge_near.is_near_duplicate("Mkrtchyan", "Karine Mkrtchyan") is True

    def test_truncated_name(self) -> None:
        """Lindsey E. Mart vs Lindsey E. Martinez."""
        assert merge_near.is_near_duplicate("Lindsey E. Mart", "Lindsey E. Martinez") is True

    def test_truncated_name_mc(self) -> None:
        """Melissa R. Mc vs Melissa R. Mccormick."""
        assert merge_near.is_near_duplicate("Melissa R. Mc", "Melissa R. Mccormick") is True

    def test_initial_vs_full_first(self) -> None:
        """R. Shawn Nelson vs Shawn Nelson — extra initial."""
        assert merge_near.is_near_duplicate("R. Shawn Nelson", "Shawn Nelson") is True

    def test_different_people(self) -> None:
        """Two different people with the same last name."""
        assert merge_near.is_near_duplicate("John A. Smith", "Robert B. Smith") is False

    def test_completely_different(self) -> None:
        assert merge_near.is_near_duplicate("John Smith", "Jane Doe") is False

    def test_empty_name(self) -> None:
        assert merge_near.is_near_duplicate("", "John Smith") is False
        assert merge_near.is_near_duplicate("John Smith", "") is False

    def test_same_first_different_last(self) -> None:
        assert merge_near.is_near_duplicate("John Smith", "John Jones") is False

    def test_craig_griffin_variants(self) -> None:
        """Craig Griffin vs Craig L. Griffin."""
        assert merge_near.is_near_duplicate("Craig Griffin", "Craig L. Griffin") is True

    def test_donald_gaffney_variants(self) -> None:
        """Donald Gaffney vs Donald F. Gaffney."""
        assert merge_near.is_near_duplicate("Donald Gaffney", "Donald F. Gaffney") is True

    def test_james_montgomery_truncated(self) -> None:
        """James I. Montgomer vs James I. Montgomery."""
        assert merge_near.is_near_duplicate("James I. Montgomer", "James I. Montgomery") is True

    def test_thomas_mcconville_truncated(self) -> None:
        """Thomas S. Mc vs Thomas S. Mcconville."""
        assert merge_near.is_near_duplicate("Thomas S. Mc", "Thomas S. Mcconville") is True

    def test_curtis_kin_truncated(self) -> None:
        """Curtis A. Kin vs Curtis A. King (hypothetical full name)."""
        assert merge_near.is_near_duplicate("Curtis A. Kin", "Curtis A. King") is True


# ---------------------------------------------------------------------------
# Survivor selection tests
# ---------------------------------------------------------------------------


class TestPickSurvivor:
    """Tests for pick_survivor()."""

    def _make_judge(
        self,
        name: str,
        ruling_count: int = 0,
        judge_id: str | None = None,
    ) -> dict[str, str | int]:
        return {
            "id": judge_id or str(uuid.uuid4()),
            "canonical_name": name,
            "court_id": str(uuid.uuid4()),
            "court_code": "ca-los-angeles",
            "ruling_count": ruling_count,
        }

    def test_prefers_more_complete_name(self) -> None:
        """The name with more words (more complete) should survive."""
        short = self._make_judge("Crowfoot", ruling_count=5)
        full = self._make_judge("William A. Crowfoot", ruling_count=2)
        survivor, dups = merge_near.pick_survivor([short, full])
        assert survivor["canonical_name"] == "William A. Crowfoot"
        assert len(dups) == 1
        assert dups[0]["canonical_name"] == "Crowfoot"

    def test_tiebreak_by_ruling_count(self) -> None:
        """When word counts are equal, prefer more rulings."""
        a = self._make_judge("Carmen R. Luege", ruling_count=10)
        b = self._make_judge("Carmen R. Luege", ruling_count=3)
        survivor, dups = merge_near.pick_survivor([a, b])
        assert survivor["id"] == a["id"]

    def test_middle_initial_preferred(self) -> None:
        """Carmen R. Luege (3 words) beats Carmen Luege (2 words)."""
        without = self._make_judge("Carmen Luege", ruling_count=10)
        with_middle = self._make_judge("Carmen R. Luege", ruling_count=2)
        survivor, dups = merge_near.pick_survivor([without, with_middle])
        assert survivor["canonical_name"] == "Carmen R. Luege"


# ---------------------------------------------------------------------------
# Group detection tests
# ---------------------------------------------------------------------------


class TestFindNearDuplicateGroups:
    """Tests for find_near_duplicate_groups()."""

    def _make_judge(
        self,
        name: str,
        court_id: str = "court-1",
        ruling_count: int = 0,
    ) -> dict[str, str | int]:
        return {
            "id": str(uuid.uuid4()),
            "canonical_name": name,
            "court_id": court_id,
            "court_code": "ca-los-angeles",
            "ruling_count": ruling_count,
        }

    def test_finds_missing_middle_initial_group(self) -> None:
        judges = [
            self._make_judge("Carmen Luege"),
            self._make_judge("Carmen R. Luege"),
            self._make_judge("John Smith"),
        ]
        groups = merge_near.find_near_duplicate_groups(judges)
        assert len(groups) == 1
        names = {str(j["canonical_name"]) for j in groups[0]}
        assert names == {"Carmen Luege", "Carmen R. Luege"}

    def test_different_courts_not_grouped(self) -> None:
        judges = [
            self._make_judge("John Smith", court_id="court-1"),
            self._make_judge("John A. Smith", court_id="court-2"),
        ]
        groups = merge_near.find_near_duplicate_groups(judges)
        assert len(groups) == 0

    def test_no_duplicates_returns_empty(self) -> None:
        judges = [
            self._make_judge("John Smith"),
            self._make_judge("Jane Doe"),
        ]
        groups = merge_near.find_near_duplicate_groups(judges)
        assert len(groups) == 0

    def test_truncated_name_group(self) -> None:
        judges = [
            self._make_judge("Lindsey E. Mart"),
            self._make_judge("Lindsey E. Martinez"),
        ]
        groups = merge_near.find_near_duplicate_groups(judges)
        assert len(groups) == 1

    def test_last_name_only_group(self) -> None:
        judges = [
            self._make_judge("Crowfoot"),
            self._make_judge("William A. Crowfoot"),
        ]
        groups = merge_near.find_near_duplicate_groups(judges)
        assert len(groups) == 1

    def test_three_way_group(self) -> None:
        """Multiple variants of the same judge form one group."""
        judges = [
            self._make_judge("Mkrtchyan"),
            self._make_judge("Karine Mkrtchyan"),
            self._make_judge("Karine Mkrtchyan"),  # exact dup too
        ]
        groups = merge_near.find_near_duplicate_groups(judges)
        assert len(groups) == 1
        assert len(groups[0]) == 3


# ---------------------------------------------------------------------------
# Database merge tests
# ---------------------------------------------------------------------------


class TestMergeJudge:
    """Tests for _merge_judge()."""

    def test_dry_run_does_not_write(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (5,)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        result = merge_near._merge_judge(conn, "from-id", "to-id", dry_run=True)
        assert result == 5
        # Should only have one cursor call (the COUNT query), not the UPDATEs
        # In dry_run mode we query count but don't execute merges
        cur.execute.assert_called_once()

    def test_merge_executes_all_statements(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        cur.rowcount = 3
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        result = merge_near._merge_judge(conn, "from-id", "to-id", dry_run=False)
        assert result == 3
        # Should execute: UPDATE rulings, UPDATE case_judges, DELETE case_judges,
        # UPDATE judge_aliases, DELETE judge_aliases, DELETE judges
        assert cur.execute.call_count == 6
