"""Tests for Orange County document splitter.

Tests cover:
  - North JC splitting: multi-case entries with "vs" pattern
  - Central/West/CM/CX splitting: case-number-based boundaries
  - Single-case PDFs: pass through unsplit
  - Edge cases: empty text, no recognizable boundaries
  - Regression tests against real OC PDF fixtures
  - Integration with the splitter framework (register_splitter)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from courts.ca.pdf_link_scraper import _extract_pdf_text
from ingestion.oc_splitter import (
    _split_case_number_based,
    _split_north,
    split_oc_document,
)
from ingestion.splitter import _splitter_registry, split_document

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# North JC splitting — synthetic tests
# ---------------------------------------------------------------------------


class TestSplitNorth:
    def test_two_entries(self) -> None:
        """Two numbered entries with 'vs' are split into two results."""
        text = (
            "# Case Name Tentative\n"
            "101 Smith vs Jones Motion for Summary Judgment\n"
            "Ruling text for Smith vs Jones.\n"
            "The motion is GRANTED.\n"
            "102 Doe vs Roe Demurrer to Complaint\n"
            "Ruling text for Doe vs Roe.\n"
            "The demurrer is OVERRULED.\n"
        )
        results = _split_north(text)
        assert len(results) == 2

        assert results[0].case_title == "Smith vs Jones"
        assert results[0].motion_type == "Motion for Summary Judgment"
        assert "GRANTED" in results[0].ruling_text
        assert results[0].case_number is None

        assert results[1].case_title == "Doe vs Roe"
        assert results[1].motion_type == "Demurrer to Complaint"
        assert "OVERRULED" in results[1].ruling_text

    def test_single_entry_returns_empty(self) -> None:
        """A single entry means no splitting needed — return empty list."""
        text = "101 Smith vs Jones Motion for Summary Judgment\nRuling text here.\n"
        results = _split_north(text)
        assert len(results) < 2

    def test_no_vs_entries_skipped(self) -> None:
        """Lines without 'vs' (legal citations) are not treated as entries."""
        text = (
            "151 Cal.App.4th 168, 175 some legal citation\n"
            "101 Smith vs Jones Motion for Summary Judgment\n"
            "Ruling for Smith.\n"
            "102 Doe vs Roe Demurrer\n"
            "Ruling for Doe.\n"
        )
        results = _split_north(text)
        assert len(results) == 2
        assert results[0].case_title == "Smith vs Jones"

    def test_empty_text(self) -> None:
        results = _split_north("")
        assert results == []

    def test_multiline_case_name(self) -> None:
        """Case name spanning multiple lines is joined correctly."""
        text = (
            "101 Careful Consulting, Motion to Be Relieved as Counsel of Record\n"
            "LLC vs Pacific Health\n"
            "Staffing, LLC\n"
            "Ruling text for Careful Consulting.\n"
            "The motion is DENIED.\n"
            "102 Alpha vs Beta Demurrer\n"
            "Ruling text for Alpha vs Beta.\n"
        )
        results = _split_north(text)
        assert len(results) == 2
        assert "vs Pacific Health" in results[0].case_title

    def test_ruling_text_includes_full_entry(self) -> None:
        """Ruling text spans from entry start to next entry start."""
        text = (
            "101 Smith vs Jones Motion for Summary Judgment\n"
            "Line 1 of ruling.\n"
            "Line 2 of ruling.\n"
            "Line 3 of ruling.\n"
            "102 Doe vs Roe Demurrer\n"
            "Line 1 of second ruling.\n"
        )
        results = _split_north(text)
        assert len(results) == 2
        assert "Line 1 of ruling" in results[0].ruling_text
        assert "Line 2 of ruling" in results[0].ruling_text
        assert "Line 3 of ruling" in results[0].ruling_text
        assert "Line 1 of second ruling" in results[1].ruling_text

    def test_split_text_shorter_than_full_page(self) -> None:
        """Each split ruling must be shorter than the full input (#1182)."""
        text = (
            "101 Smith vs Jones Motion for Summary Judgment\n"
            "Ruling text for Smith vs Jones.\n"
            "The motion is GRANTED.\n"
            "102 Doe vs Roe Demurrer to Complaint\n"
            "Ruling text for Doe vs Roe.\n"
            "The demurrer is OVERRULED.\n"
        )
        results = _split_north(text)
        full_len = len(text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len, (
                f"Split [{i}] has {len(r.ruling_text)} chars, same as full text ({full_len})"
            )


# ---------------------------------------------------------------------------
# Case-number-based splitting — synthetic tests
# ---------------------------------------------------------------------------


class TestSplitCaseNumberBased:
    def test_two_entries_with_case_numbers(self) -> None:
        """Two numbered entries with case numbers are split correctly."""
        text = (
            "TENTATIVE RULINGS\n"
            "DEPT C25\n"
            "Date: March 10, 2026\n"
            "# Case Name Tentative\n"
            "1. Smith vs. Jones Motion for Summary Judgment\n"
            "25-01455183\n"
            "The motion is GRANTED.\n"
            "Detailed ruling text here.\n"
            "2. Doe vs. Roe Demurrer\n"
            "24-01428812\n"
            "The demurrer is OVERRULED.\n"
        )
        results = _split_case_number_based(text)
        assert len(results) == 2

        assert results[0].case_number == "25-01455183"
        assert "GRANTED" in results[0].ruling_text
        assert results[0].case_title is not None
        assert "Smith" in results[0].case_title

        assert results[1].case_number == "24-01428812"
        assert "OVERRULED" in results[1].ruling_text

    def test_three_part_case_numbers(self) -> None:
        """Three-part case numbers (30-2024-01393434) are captured."""
        text = (
            "# Case Name Tentative\n"
            "1 30-2024-01393434 1. Case Management Conference\n"
            "Some ruling text.\n"
            "3. 30-2024-01447336 1. Motion to Be Relieved\n"
            "Another ruling text.\n"
        )
        results = _split_case_number_based(text)
        assert len(results) == 2
        assert results[0].case_number == "30-2024-01393434"
        assert results[1].case_number == "30-2024-01447336"

    def test_single_entry_returns_empty(self) -> None:
        text = (
            "1. Smith vs. Jones Motion for Summary Judgment\n25-01455183\nThe motion is GRANTED.\n"
        )
        results = _split_case_number_based(text)
        assert len(results) < 2

    def test_no_case_numbers_returns_empty(self) -> None:
        text = "Some random text without case numbers or entries.\n"
        results = _split_case_number_based(text)
        assert results == []

    def test_empty_text(self) -> None:
        results = _split_case_number_based("")
        assert results == []

    def test_entries_without_case_numbers_but_with_vs(self) -> None:
        """Entries identified by 'vs' when case numbers are absent."""
        text = (
            "100 Smith vs. Jones HEARING ADVANCED\n"
            "Ruling text for case 1.\n"
            "101 Doe vs. Roe OFF CALENDAR\n"
            "Some text for case 2.\n"
        )
        results = _split_case_number_based(text)
        assert len(results) == 2

    def test_motion_type_extracted(self) -> None:
        """Motion type is parsed from the first line of the entry."""
        text = (
            "1. Smith vs. Jones Motion for Summary Judgment\n"
            "25-01455183\n"
            "Ruling text.\n"
            "2. Doe vs. Roe Demurrer to Complaint\n"
            "24-01428812\n"
            "More ruling text.\n"
        )
        results = _split_case_number_based(text)
        assert len(results) == 2
        assert results[0].motion_type == "Motion for Summary Judgment"
        assert results[1].motion_type == "Demurrer to Complaint"

    def test_split_text_shorter_than_full_page(self) -> None:
        """Each split ruling must be shorter than the full input (#1182)."""
        text = (
            "TENTATIVE RULINGS\n"
            "DEPT C25\n"
            "1. Smith vs. Jones Motion for Summary Judgment\n"
            "25-01455183\n"
            "Detailed ruling text for the first case.\n"
            "2. Doe vs. Roe Demurrer\n"
            "24-01428812\n"
            "Detailed ruling text for the second case.\n"
        )
        results = _split_case_number_based(text)
        full_len = len(text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len, (
                f"Split [{i}] has {len(r.ruling_text)} chars, same as full text ({full_len})"
            )


# ---------------------------------------------------------------------------
# split_oc_document — dispatch tests
# ---------------------------------------------------------------------------


class TestSplitOcDocument:
    def test_north_dept_dispatches_to_north_splitter(self) -> None:
        text = (
            "101 Smith vs Jones Motion for Summary Judgment\n"
            "Ruling text.\n"
            "102 Doe vs Roe Demurrer\n"
            "More ruling text.\n"
        )
        event = {"ruling_text": text, "department": "N14"}
        results = split_oc_document(event)
        assert len(results) == 2
        assert results[0].case_number is None  # North has no case numbers

    def test_central_dept_dispatches_to_case_number_splitter(self) -> None:
        text = (
            "1. Smith vs. Jones Motion for Summary Judgment\n"
            "25-01455183\n"
            "Ruling text.\n"
            "2. Doe vs. Roe Demurrer\n"
            "24-01428812\n"
            "More text.\n"
        )
        event = {"ruling_text": text, "department": "C25"}
        results = split_oc_document(event)
        assert len(results) == 2
        assert results[0].case_number == "25-01455183"

    def test_empty_ruling_text(self) -> None:
        event = {"ruling_text": "", "department": "C25"}
        results = split_oc_document(event)
        assert results == []

    def test_none_ruling_text(self) -> None:
        event = {"ruling_text": None, "department": "C25"}
        results = split_oc_document(event)
        assert results == []

    def test_no_department_uses_case_number_splitter(self) -> None:
        """When no department is provided, defaults to case-number-based."""
        text = (
            "1. Smith vs. Jones Motion for Summary Judgment\n"
            "25-01455183\n"
            "Ruling text.\n"
            "2. Doe vs. Roe Demurrer\n"
            "24-01428812\n"
            "More text.\n"
        )
        event = {"ruling_text": text, "department": None}
        results = split_oc_document(event)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Framework integration — split_document() dispatches to OC splitter
# ---------------------------------------------------------------------------


class TestFrameworkIntegration:
    def test_registered_in_splitter_registry(self) -> None:
        """OC splitter is registered for CA/Orange."""
        assert ("CA", "Orange") in _splitter_registry

    def test_split_document_dispatches_to_oc(self) -> None:
        """split_document() uses the OC splitter for CA/Orange events."""
        text = (
            "101 Smith vs Jones Motion for Summary Judgment\n"
            "Ruling text.\n"
            "102 Doe vs Roe Demurrer\n"
            "More ruling text.\n"
        )
        event = {
            "state": "CA",
            "county": "Orange",
            "ruling_text": text,
            "department": "N14",
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) == 2
        assert results[0].case_title == "Smith vs Jones"


# ---------------------------------------------------------------------------
# Regression tests against real PDF fixtures
# ---------------------------------------------------------------------------


class TestNorthFixture:
    """Regression tests using the real North JC PDF fixture (oc_north_n.pdf)."""

    @pytest.fixture()
    def north_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_north_n.pdf"))

    def test_splits_multiple_cases(self, north_text: str) -> None:
        results = _split_north(north_text)
        # North fixture has ~11 case entries
        assert len(results) >= 10

    def test_alday_entry(self, north_text: str) -> None:
        """Entry 101: Alday vs Orange Coast Title."""
        results = _split_north(north_text)
        alday = [r for r in results if "Alday" in (r.case_title or "")]
        assert len(alday) == 1
        assert "Continuance" in (alday[0].motion_type or "")
        assert (
            "continued" in alday[0].ruling_text.lower()
            or "Trial is continued" in alday[0].ruling_text
        )

    def test_each_entry_has_unique_ruling_text(self, north_text: str) -> None:
        """Each split result should have distinct ruling text."""
        results = _split_north(north_text)
        texts = [r.ruling_text for r in results]
        # No two results should have identical text
        assert len(set(texts)) == len(texts)

    def test_split_text_shorter_than_full_page(self, north_text: str) -> None:
        """Each split ruling must be shorter than the full page (#1182)."""
        results = _split_north(north_text)
        full_len = len(north_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len, (
                f"Split [{i}] has {len(r.ruling_text)} chars, "
                f"same as full page ({full_len}). "
                "Split ruling should contain only case-specific text."
            )

    def test_all_entries_have_case_title_with_vs(self, north_text: str) -> None:
        results = _split_north(north_text)
        for r in results:
            assert r.case_title is not None
            assert "vs" in r.case_title.lower(), f"Entry missing 'vs' in title: {r.case_title}"

    def test_split_via_framework(self, north_text: str) -> None:
        """Verify the full framework pipeline works for North JC."""
        event = {
            "state": "CA",
            "county": "Orange",
            "ruling_text": north_text,
            "department": "N14",
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) >= 10


class TestCentralFixture:
    """Regression tests using the Central JC fixture (oc_central_c34.pdf)."""

    @pytest.fixture()
    def central_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_central_c34.pdf"))

    def test_splits_multiple_cases(self, central_text: str) -> None:
        results = _split_case_number_based(central_text)
        # C34 has 11 case numbers
        assert len(results) >= 8

    def test_first_entry_has_case_number(self, central_text: str) -> None:
        results = _split_case_number_based(central_text)
        assert results[0].case_number is not None
        assert "2024-01393434" in results[0].case_number

    def test_each_entry_has_case_number(self, central_text: str) -> None:
        results = _split_case_number_based(central_text)
        for r in results:
            assert r.case_number is not None, (
                f"Entry missing case number. Title: {r.case_title}, "
                f"Text start: {r.ruling_text[:80]}"
            )

    def test_sub_motions_not_split_as_separate_entries(self, central_text: str) -> None:
        """Sub-motion numbered items (e.g. '2. Status Conference') should not
        be treated as separate case entries."""
        results = _split_case_number_based(central_text)
        # C34 has 11 distinct cases; without sub-motion filtering we'd get more
        assert len(results) == 11

    def test_split_text_shorter_than_full_page(self, central_text: str) -> None:
        """Each split ruling must be shorter than the full page (#1182)."""
        results = _split_case_number_based(central_text)
        full_len = len(central_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len * 0.5, (
                f"Split [{i}] (case={r.case_number}) has {len(r.ruling_text)} chars, "
                f">= 50% of full page ({full_len}). Should be case-specific text only."
            )


class TestApkarianFixture:
    """Regression tests using Apkarian C25 fixture."""

    @pytest.fixture()
    def apkarian_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_apkarian_c25.pdf"))

    def test_splits_multiple_cases(self, apkarian_text: str) -> None:
        results = _split_case_number_based(apkarian_text)
        # C25 has 14 case numbers
        assert len(results) >= 10

    def test_first_entry_is_okino(self, apkarian_text: str) -> None:
        results = _split_case_number_based(apkarian_text)
        # First entry should be Okino vs. Ashe with case number 25-01455183
        assert results[0].case_number == "25-01455183"

    def test_no_entry_contains_other_case_text(self, apkarian_text: str) -> None:
        """No split ruling should contain the first line of another entry (#1182).

        This is a generalized version of the Zavala v Becker check — verifies
        that boundary detection correctly isolates each case's text.
        """
        results = _split_case_number_based(apkarian_text)
        for i, r in enumerate(results):
            first_line = r.ruling_text.split("\n")[0].strip()
            if not first_line:
                continue
            for j, other in enumerate(results):
                if i == j:
                    continue
                assert first_line not in other.ruling_text, (
                    f"First line of split [{i}] found in split [{j}]. "
                    f"Entries should not overlap. Line: {first_line!r}"
                )

    def test_split_text_shorter_than_full_page(self, apkarian_text: str) -> None:
        """Each split ruling must be shorter than the full page (#1182).

        Regression test for the bug where split rulings retained the full
        combined calendar page text instead of case-specific excerpts.
        """
        results = _split_case_number_based(apkarian_text)
        full_len = len(apkarian_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len * 0.5, (
                f"Split [{i}] (case={r.case_number}) has {len(r.ruling_text)} chars, "
                f"which is >= 50% of the full page ({full_len} chars). "
                "Split rulings should contain only case-specific text, not the full page."
            )

    def test_split_texts_do_not_overlap_across_cases(self, apkarian_text: str) -> None:
        """No two split results should share significant text (#1182).

        Verifies that boundaries are correctly identified and each ruling
        contains only its own case text, not text from adjacent cases.
        """
        results = _split_case_number_based(apkarian_text)
        # Check that the first line of each result does NOT appear in any other result.
        # This verifies non-overlapping boundaries.
        for i, r in enumerate(results):
            first_line = r.ruling_text.split("\n")[0].strip()
            if not first_line:
                continue
            for j, other in enumerate(results):
                if i == j:
                    continue
                assert first_line not in other.ruling_text, (
                    f"First line of split [{i}] found in split [{j}]. "
                    f"Entries should not overlap. Line: {first_line!r}"
                )


class TestWestFixture:
    """Regression tests using West JC fixture (oc_west_w.pdf)."""

    @pytest.fixture()
    def west_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_west_w.pdf"))

    def test_splits_multiple_cases(self, west_text: str) -> None:
        results = _split_case_number_based(west_text)
        # West has 10 case numbers
        assert len(results) >= 5

    def test_first_entry_has_case_number(self, west_text: str) -> None:
        results = _split_case_number_based(west_text)
        assert results[0].case_number is not None

    def test_split_text_shorter_than_full_page(self, west_text: str) -> None:
        """Each split ruling must be shorter than the full page (#1182)."""
        results = _split_case_number_based(west_text)
        full_len = len(west_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len * 0.5, (
                f"Split [{i}] (case={r.case_number}) has {len(r.ruling_text)} chars, "
                f">= 50% of full page ({full_len}). Should be case-specific text only."
            )


class TestCostaMesaFixture:
    """Regression tests using Costa Mesa fixture (oc_costa_mesa_cm.pdf)."""

    @pytest.fixture()
    def cm_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_costa_mesa_cm.pdf"))

    def test_splits_multiple_cases(self, cm_text: str) -> None:
        results = _split_case_number_based(cm_text)
        # Costa Mesa has 5 case numbers
        assert len(results) >= 3

    def test_first_entry_has_case_number(self, cm_text: str) -> None:
        results = _split_case_number_based(cm_text)
        assert results[0].case_number is not None
        assert "2024-01437598" in results[0].case_number

    def test_split_text_shorter_than_full_page(self, cm_text: str) -> None:
        """Each split ruling must be shorter than the full page (#1182)."""
        results = _split_case_number_based(cm_text)
        full_len = len(cm_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len * 0.5, (
                f"Split [{i}] (case={r.case_number}) has {len(r.ruling_text)} chars, "
                f">= 50% of full page ({full_len}). Should be case-specific text only."
            )


class TestComplexFixture:
    """Regression tests using Complex fixture (oc_complex_cx.pdf)."""

    @pytest.fixture()
    def cx_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_complex_cx.pdf"))

    def test_splits_multiple_cases(self, cx_text: str) -> None:
        results = _split_case_number_based(cx_text)
        # Complex has 9 case numbers
        assert len(results) >= 5

    def test_first_entry_has_case_number(self, cx_text: str) -> None:
        results = _split_case_number_based(cx_text)
        assert results[0].case_number is not None
        assert "2023-01301305" in results[0].case_number

    def test_split_text_shorter_than_full_page(self, cx_text: str) -> None:
        """Each split ruling must be shorter than the full page (#1182)."""
        results = _split_case_number_based(cx_text)
        full_len = len(cx_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len * 0.5, (
                f"Split [{i}] (case={r.case_number}) has {len(r.ruling_text)} chars, "
                f">= 50% of full page ({full_len}). Should be case-specific text only."
            )


# ---------------------------------------------------------------------------
# Cross-fixture text length regression tests (#1182)
# ---------------------------------------------------------------------------


class TestSplitRulingTextLengths:
    """Regression tests verifying split rulings contain case-specific text,
    not the full calendar page (#1182).

    Root cause: between PRs #1156 and #1162, the OC splitter code existed but
    was not registered at import time. Documents processed during that window
    were stored unsplit with full page text. The splitter registration was
    fixed in #1162 (``_load_county_splitters()``). These tests guard against
    regressions in the splitting logic that could cause split results to
    retain the full page text.
    """

    @pytest.fixture()
    def north_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_north_n.pdf"))

    @pytest.fixture()
    def central_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_central_c34.pdf"))

    @pytest.fixture()
    def apkarian_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_apkarian_c25.pdf"))

    def test_north_splits_sum_to_less_than_full_page(self, north_text: str) -> None:
        """Total split text should not exceed the full page (no duplication)."""
        results = _split_north(north_text)
        total_split_len = sum(len(r.ruling_text) for r in results)
        # Allow some overhead from shared header lines
        assert total_split_len <= len(north_text) * 1.1, (
            f"Total split text ({total_split_len}) exceeds full page ({len(north_text)}) "
            "by more than 10%. Splits may be duplicating content."
        )

    def test_central_splits_sum_to_less_than_full_page(self, central_text: str) -> None:
        """Total split text should not exceed the full page (no duplication)."""
        results = _split_case_number_based(central_text)
        total_split_len = sum(len(r.ruling_text) for r in results)
        assert total_split_len <= len(central_text) * 1.1, (
            f"Total split text ({total_split_len}) exceeds full page ({len(central_text)}) "
            "by more than 10%. Splits may be duplicating content."
        )

    def test_framework_split_produces_case_specific_text(self, apkarian_text: str) -> None:
        """End-to-end: split_document() through the framework produces
        case-specific text, not full page text (#1182)."""
        event = {
            "state": "CA",
            "county": "Orange",
            "ruling_text": apkarian_text,
            "department": "C25",
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) >= 2, "Expected multi-case document to be split"

        full_len = len(apkarian_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len * 0.5, (
                f"Framework split [{i}] has {len(r.ruling_text)} chars, "
                f">= 50% of full page ({full_len}). "
                "The split ruling should contain only case-specific text."
            )


class TestSingleCasePassthrough:
    """Single-case PDFs should not be split."""

    def test_single_north_entry_not_split(self) -> None:
        text = "101 Smith vs Jones Motion for Summary Judgment\nThe motion is GRANTED.\n"
        event = {
            "state": "CA",
            "county": "Orange",
            "ruling_text": text,
            "department": "N14",
            "case_title": "Smith vs Jones",
            "case_number": None,
            "motion_type": "Motion for Summary Judgment",
            "outcome": None,
        }
        results = split_document(event)
        # Single entry means no splitting — pass through
        assert len(results) == 1
        assert results[0].ruling_text == text.rstrip()  # from splitter
        # With only 1 result, framework returns it unchanged

    def test_single_central_entry_not_split(self) -> None:
        text = (
            "1. Smith vs. Jones Motion for Summary Judgment\n25-01455183\nThe motion is GRANTED.\n"
        )
        event = {
            "state": "CA",
            "county": "Orange",
            "ruling_text": text,
            "department": "C25",
            "case_title": None,
            "case_number": "25-01455183",
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) == 1
