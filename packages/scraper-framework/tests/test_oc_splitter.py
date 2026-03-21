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

import re
from pathlib import Path

import pytest

from courts.ca.pdf_link_scraper import _extract_pdf_text
from ingestion.oc_splitter import (
    _clean_case_title,
    _extract_case_title_from_entry,
    _extract_leading_name_fragment,
    _extract_multiline_case_title,
    _is_boilerplate_only,
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
# Boilerplate detection tests
# ---------------------------------------------------------------------------


class TestIsBoilerplateOnly:
    """Tests for the _is_boilerplate_only() helper."""

    def test_header_only_text_is_boilerplate(self) -> None:
        """Calendar header with no substantive content is boilerplate."""
        text = (
            "TENTATIVE RULINGS\n"
            "LAW & MOTION CALENDAR\n"
            "DEPT N17\n"
            "Judge Craig L. Griffin\n"
            "Date: March 16, 2026\n"
            "Time: 2:00 PM\n"
            "#\n"
        )
        assert _is_boilerplate_only(text) is True

    def test_header_with_submit_instructions_is_boilerplate(self) -> None:
        text = (
            "TENTATIVE RULINGS\n"
            "LAW & MOTION\n"
            "DEPT C25\n"
            "Judge Gassia Apkarian\n"
            "Date: March 10, 2026\n"
            "If you are submitting to the tentative, please call the Clerk.\n"
        )
        assert _is_boilerplate_only(text) is True

    def test_ruling_text_is_not_boilerplate(self) -> None:
        """Actual ruling content is not boilerplate."""
        text = (
            "101 Smith vs Jones Motion for Summary Judgment\n"
            "The Court has reviewed the moving papers, opposition, and reply.\n"
            "The motion is GRANTED. Defendant has failed to raise a triable\n"
            "issue of material fact.\n"
        )
        assert _is_boilerplate_only(text) is False

    def test_short_substantive_text_is_not_boilerplate(self) -> None:
        """Even short ruling text with real content is not boilerplate."""
        text = "The motion is GRANTED. Defendant to give notice of this ruling."
        assert _is_boilerplate_only(text) is False

    def test_empty_text_is_boilerplate(self) -> None:
        assert _is_boilerplate_only("") is True

    def test_only_whitespace_is_boilerplate(self) -> None:
        assert _is_boilerplate_only("   \n  \n  ") is True

    def test_page_numbers_only_without_marker_is_not_boilerplate(self) -> None:
        """Page numbers without a boilerplate marker are not classified as boilerplate.

        The implementation requires at least one boilerplate marker (e.g.
        'TENTATIVE RULINGS') to be present before classifying text as
        boilerplate-only.  This prevents filtering legitimate short rulings.
        """
        text = "Page 1 of 12\n\nPage 2 of 12\n"
        assert _is_boilerplate_only(text) is False

    def test_judge_prose_line_is_not_boilerplate(self) -> None:
        """A line like 'Judge Smith's prior ruling is instructive' is NOT boilerplate."""
        text = "Judge Smith's prior ruling is instructive and controls the outcome here."
        assert _is_boilerplate_only(text) is False

    def test_dept_in_prose_is_not_boilerplate(self) -> None:
        """A line mentioning a department in context is NOT boilerplate."""
        text = "The case was transferred from Dept C25 to the current department for trial."
        assert _is_boilerplate_only(text) is False


class TestBoilerplateFiltering:
    """Tests that boilerplate-only fragments are filtered from split results."""

    def test_north_keeps_entry_with_boilerplate_and_case_line(self) -> None:
        """North splitter keeps entries that have a case entry line plus boilerplate.

        The current _is_boilerplate_only implementation requires ALL non-blank
        lines to be boilerplate/metadata.  A case entry line like "1 A vs B"
        is not classified as boilerplate, so the entry is kept.
        """
        text = (
            "1 A vs B\n"
            "TENTATIVE RULINGS\n"
            "LAW & MOTION CALENDAR\n"
            "DEPT N17\n"
            "Judge Craig L. Griffin\n"
            "2 Post v. Chung Motion for Summary Judgment\n"
            "The motion is DENIED as defendant has failed to meet its burden.\n"
        )
        results = _split_north(text)
        # Both entries are kept — "1 A vs B" line is non-boilerplate.
        assert len(results) == 2

    def test_north_keeps_all_real_entries(self) -> None:
        """North splitter preserves all entries with real ruling content."""
        text = (
            "1 Zavala vs. Becker Motion for Attorneys' Fees\n"
            "The Court will GRANT IN PART the motion.\n"
            "Defendants are to give notice of this ruling.\n"
            "2 Post v. Chung Motion for Summary Judgment\n"
            "The motion is DENIED as defendant has failed to meet its burden.\n"
        )
        results = _split_north(text)
        assert len(results) == 2
        for r in results:
            assert not _is_boilerplate_only(r.ruling_text)

    def test_case_number_based_keeps_entry_with_number_and_boilerplate(self) -> None:
        """Case-number splitter keeps entries that have non-boilerplate lines.

        The _is_boilerplate_only implementation requires ALL non-blank lines
        to be boilerplate/metadata.  Lines like "1 25-01455183" are not
        classified as boilerplate, so the entry is kept.
        """
        text = (
            "1 25-01455183\n"
            "TENTATIVE RULINGS\n"
            "LAW & MOTION CALENDAR\n"
            "DEPT C25\n"
            "Judge Gassia Apkarian\n"
            "2 Smith vs. Jones Motion for Summary Judgment\n"
            "24-01428812\n"
            "The motion is GRANTED.\n"
            "Defendant's motion is without merit.\n"
        )
        results = _split_case_number_based(text)
        # Both entries are kept — non-boilerplate lines prevent filtering.
        assert len(results) == 2


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
        assert results == []

    def test_single_surviving_entry_returns_empty(self) -> None:
        """If only 1 entry survives filtering, return empty (#1232).

        This covers the N18-like scenario where legal citations containing
        'v.' create a single false-positive entry.  The < 2 minimum ensures
        the caller falls through to a different splitting strategy.
        """
        text = (
            "Some introductory boilerplate text.\n"
            "24 Cal.4th 83, 114; Mercuro v. Superior Court (2002) 96\n"
            "Cal.App.4th 167, 174-175.\n"
            "Procedural Unconscionability analysis continues here.\n"
        )
        results = _split_north(text)
        assert results == []

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
# North JC short-number splitting — 1-2 digit entry numbers (#1093)
# ---------------------------------------------------------------------------


class TestSplitNorthShortNumbers:
    """Tests for North JC departments that use 1-2 digit entry numbers (N15-N18)."""

    def test_single_digit_entries(self) -> None:
        """1-digit entry numbers (N17 format) are split correctly."""
        text = (
            "TENTATIVE RULINGS\n"
            "Judge Craig L. Griffin\n"
            "Date: March 16, 2026\n"
            "#\n"
            "1 Zavala vs. Motion for Attorneys' Fees and Costs\n"
            "Becker et al.\n"
            "The Court will GRANT IN PART the motion.\n"
            "Defendants are to give notice of this ruling.\n"
            "2 Post vs. Chung The motion for summary judgment is DENIED.\n"
            "As the party moving for summary judgment, defendant has the\n"
            "burden to show it is entitled to judgment.\n"
            "3 Alpha vs Beta Demurrer to Complaint\n"
            "The demurrer is SUSTAINED.\n"
        )
        results = _split_north(text)
        assert len(results) == 3

        assert results[0].case_title is not None
        assert "Zavala" in results[0].case_title
        assert "GRANT" in results[0].ruling_text
        # Ruling text should NOT contain other cases' text
        assert "Post vs. Chung" not in results[0].ruling_text

        assert results[1].case_title is not None
        assert "Post" in results[1].case_title
        assert "DENIED" in results[1].ruling_text

        assert results[2].case_title is not None
        assert "Alpha" in results[2].case_title

    def test_two_digit_entries(self) -> None:
        """2-digit entry numbers are split correctly."""
        text = (
            "10 Smith vs Jones Motion for Summary Judgment\n"
            "The motion is GRANTED.\n"
            "11 Doe vs Roe Demurrer\n"
            "The demurrer is OVERRULED.\n"
        )
        results = _split_north(text)
        assert len(results) == 2
        assert results[0].case_title == "Smith vs Jones"
        assert results[1].case_title == "Doe vs Roe"

    def test_mixed_digit_lengths(self) -> None:
        """Mix of 1-digit and 2-digit entries are split correctly."""
        text = (
            "1 Smith vs Jones Motion to Compel\n"
            "Ruling for Smith.\n"
            "2 Doe vs Roe Demurrer\n"
            "Ruling for Doe.\n"
            "10 Alpha vs Beta Application\n"
            "Ruling for Alpha.\n"
        )
        results = _split_north(text)
        assert len(results) == 3

    def test_single_digit_single_entry_returns_empty(self) -> None:
        """A single 1-digit entry means no splitting needed."""
        text = "1 Smith vs Jones Motion for Summary Judgment\nRuling text here.\n"
        results = _split_north(text)
        assert len(results) < 2

    def test_short_number_split_text_shorter_than_full(self) -> None:
        """Each split ruling from short-number entries must be shorter than full text."""
        text = (
            "1 Zavala vs. Becker Motion for Attorneys' Fees\n"
            "The Court will GRANT IN PART.\n"
            "2 Post v. Chung Motion for Summary Judgment\n"
            "The motion is DENIED.\n"
        )
        results = _split_north(text)
        full_len = len(text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len, (
                f"Split [{i}] has {len(r.ruling_text)} chars, same as full text ({full_len})"
            )

    def test_short_number_with_multiline_case_name(self) -> None:
        """Short-number entries with multi-line case names work correctly."""
        text = (
            "1 Zavala vs. Motion for Attorneys' Fees and Costs\n"
            "Becker et al.\n"
            "The Court will GRANT IN PART.\n"
            "2 Post vs. Chung Motion for Summary Judgment\n"
            "The motion is DENIED.\n"
        )
        results = _split_north(text)
        assert len(results) == 2
        assert "Zavala" in results[0].case_title

    def test_v_dot_separator(self) -> None:
        """'v.' (common legal abbreviation) is recognized as a case separator."""
        text = (
            "1 Zavala vs. Becker Motion for Attorneys' Fees\n"
            "The Court will GRANT IN PART.\n"
            "2 Post v. Chung Motion for Summary Judgment\n"
            "The motion is DENIED.\n"
            "3 Alpha vs Beta Demurrer\n"
            "The demurrer is OVERRULED.\n"
        )
        results = _split_north(text)
        assert len(results) == 3
        assert "Zavala" in results[0].case_title
        assert "Post" in results[1].case_title
        assert "Alpha" in results[2].case_title

    def test_legal_citation_not_matched(self) -> None:
        """Numbers in legal citations (e.g. '45 Cal.App.4th') are not entries."""
        text = (
            "1 Smith vs Jones Motion for Summary Judgment\n"
            "See 45 Cal.App.4th 705, 717 for the applicable standard.\n"
            "The motion is GRANTED.\n"
            "2 Doe vs Roe Demurrer\n"
            "The demurrer is OVERRULED.\n"
        )
        results = _split_north(text)
        assert len(results) == 2
        # The "45 Cal.App.4th" line should NOT create a third entry
        assert all("Cal.App" not in (r.case_title or "") for r in results)


# ---------------------------------------------------------------------------
# North JC prose-number pre-filter — regression tests (#1186)
# ---------------------------------------------------------------------------


class TestSplitNorthProseFilter:
    """Tests that prose lines starting with numbers do not corrupt boundaries.

    The root cause of #1186: with \\d{1,3}, lines like "10 calendar days" were
    treated as entry boundaries, splitting ruling text at the wrong positions.
    The _is_north_case_entry() pre-filter checks for "vs" nearby before
    accepting a numbered line as a case entry boundary.
    """

    ZAVALA_STYLE_TEXT = (
        "TENTATIVE RULINGS\n"
        "Judge Craig L. Griffin\n"
        "Date: March 16, 2026\n"
        "Time: 2:00 PM\n"
        "If you are submitting to the tentative, please call the Clerk.\n"
        "#\n"
        "1 Zavala vs. Before the Court is the Motion for Attorneys' Fees\n"
        "Becker et al. filed by Karl Mark Becker.\n"
        "\n"
        "Plaintiff Riordan Zavala has belatedly filed an Opposition.\n"
        "The Court therefore will not consider Plaintiff's untimely Opposition.\n"
        "The Motion seeks attorneys' fees of $15,000.\n"
        "Tentative Ruling: The Motion for Attorneys' Fees is GRANTED.\n"
        "2 Alpha Corp vs. Motion to Compel Discovery\n"
        "Beta LLC\n"
        "\n"
        "Defendant has failed to respond to discovery requests.\n"
        "10 calendar days is insufficient notice.\n"
        "Tentative Ruling: The Motion to Compel is GRANTED.\n"
        "3 Gamma vs. Delta Demurrer to Complaint\n"
        "\n"
        "The Demurrer is SUSTAINED with 20 days leave to amend.\n"
    )

    def test_splits_three_single_digit_entries(self) -> None:
        """Three single-digit entries with 'vs' are split correctly."""
        results = _split_north(self.ZAVALA_STYLE_TEXT)
        assert len(results) == 3

    def test_first_entry_is_zavala(self) -> None:
        results = _split_north(self.ZAVALA_STYLE_TEXT)
        assert results[0].case_title is not None
        assert "Zavala" in results[0].case_title

    def test_zavala_ruling_text_contains_fees(self) -> None:
        results = _split_north(self.ZAVALA_STYLE_TEXT)
        assert "Attorneys' Fees is GRANTED" in results[0].ruling_text

    def test_zavala_ruling_does_not_contain_other_cases(self) -> None:
        """Zavala ruling text should not contain Alpha or Gamma text."""
        results = _split_north(self.ZAVALA_STYLE_TEXT)
        assert "Alpha Corp" not in results[0].ruling_text
        assert "Gamma vs" not in results[0].ruling_text

    def test_each_split_shorter_than_full_text(self) -> None:
        results = _split_north(self.ZAVALA_STYLE_TEXT)
        full_len = len(self.ZAVALA_STYLE_TEXT)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len, (
                f"Split [{i}] has {len(r.ruling_text)} chars, same as full text ({full_len})"
            )

    def test_prose_numbers_not_treated_as_entries(self) -> None:
        """Lines like '10 calendar days' should not be treated as entries."""
        results = _split_north(self.ZAVALA_STYLE_TEXT)
        # "10 calendar days" appears in Alpha Corp's ruling but should not
        # create a separate entry or split boundary.
        entry_nums = [r.ruling_text.split("\n")[0] for r in results]
        for line in entry_nums:
            assert "calendar days" not in line

    def test_alpha_ruling_contains_calendar_days_line(self) -> None:
        """The '10 calendar days' line belongs to Alpha Corp's ruling, not a new entry."""
        results = _split_north(self.ZAVALA_STYLE_TEXT)
        alpha = [r for r in results if "Alpha" in (r.case_title or "")]
        assert len(alpha) == 1
        assert "10 calendar days" in alpha[0].ruling_text

    def test_motion_type_extracted(self) -> None:
        results = _split_north(self.ZAVALA_STYLE_TEXT)
        alpha = [r for r in results if "Alpha" in (r.case_title or "")]
        assert len(alpha) == 1
        assert alpha[0].motion_type is not None
        assert "Motion to Compel" in alpha[0].motion_type

    def test_framework_dispatch_for_north_dept(self) -> None:
        """split_oc_document dispatches to North splitter for N17."""
        event = {"ruling_text": self.ZAVALA_STYLE_TEXT, "department": "N17"}
        results = split_oc_document(event)
        assert len(results) == 3
        assert results[0].case_number is None  # North has no case numbers

    def test_framework_integration_end_to_end(self) -> None:
        """split_document() through the framework splits N17 single-digit entries."""
        event = {
            "state": "CA",
            "county": "Orange",
            "ruling_text": self.ZAVALA_STYLE_TEXT,
            "department": "N17",
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) == 3
        assert "Zavala" in (results[0].case_title or "")


class TestSplitOcDocumentFallback:
    """Tests for the North-to-case-number fallback in split_oc_document."""

    def test_north_dept_falls_back_when_north_returns_empty(self) -> None:
        """If North splitter returns empty, fall back to case-number-based."""
        # Text with case numbers but no "vs" — North splitter filters by "vs"
        # so it returns empty, triggering fallback to case-number-based.
        text = (
            "1 30-2024-01393434 Creditors Adjustment Bureau\n"
            "Matter is continued to April 15.\n"
            "2 30-2024-01447336 National Default Servicing\n"
            "Matter is continued to May 1.\n"
        )
        event = {"ruling_text": text, "department": "N14"}
        results = split_oc_document(event)
        assert len(results) == 2
        assert results[0].case_number is not None


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

    def test_case_titles_are_clean(self, north_text: str) -> None:
        """Case titles should be clean names, not garbled with ruling text (#1357)."""
        results = _split_north(north_text)
        ruling_keywords = ["granted", "denied", "relief"]
        for r in results:
            title = r.case_title or ""
            title_lower = title.lower()
            for kw in ruling_keywords:
                assert kw not in title_lower, (
                    f"Case title contains ruling text keyword {kw!r}: "
                    f"{title!r} (case_number={r.case_number})"
                )
            assert len(title) <= 80, (
                f"Case title too long (likely garbled): "
                f"{title!r} (len={len(title)}, case_number={r.case_number})"
            )
            # Titles containing 'vs' should have a non-empty defendant name
            vs_match = re.search(r"\b(?:vs\.?|v\.)\s*$", title, re.IGNORECASE)
            if vs_match and "vs" in title_lower:
                assert False, (
                    f"Case title ends with 'vs' and no defendant: "
                    f"{title!r} (case_number={r.case_number})"
                )

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

    def test_case_titles_are_clean(self, central_text: str) -> None:
        """Case titles should be clean names, not garbled with ruling text (#1357)."""
        results = _split_case_number_based(central_text)
        ruling_keywords = ["granted", "denied", "relief"]
        for r in results:
            title = r.case_title or ""
            title_lower = title.lower()
            for kw in ruling_keywords:
                assert kw not in title_lower, (
                    f"Case title contains ruling text keyword {kw!r}: "
                    f"{title!r} (case_number={r.case_number})"
                )
            assert len(title) <= 80, (
                f"Case title too long (likely garbled): "
                f"{title!r} (len={len(title)}, case_number={r.case_number})"
            )
            # Titles containing 'vs' should have a non-empty defendant name
            vs_match = re.search(r"\b(?:vs\.?|v\.)\s*$", title, re.IGNORECASE)
            if vs_match and "vs" in title_lower:
                assert False, (
                    f"Case title ends with 'vs' and no defendant: "
                    f"{title!r} (case_number={r.case_number})"
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

    def test_case_titles_are_clean(self, apkarian_text: str) -> None:
        """Case titles should be clean names, not garbled with ruling text (#1357)."""
        results = _split_case_number_based(apkarian_text)
        ruling_keywords = ["granted", "denied", "relief"]
        for r in results:
            title = r.case_title or ""
            title_lower = title.lower()
            for kw in ruling_keywords:
                assert kw not in title_lower, (
                    f"Case title contains ruling text keyword {kw!r}: "
                    f"{title!r} (case_number={r.case_number})"
                )
            assert len(title) <= 80, (
                f"Case title too long (likely garbled): "
                f"{title!r} (len={len(title)}, case_number={r.case_number})"
            )
            # Titles containing 'vs' should have a non-empty defendant name
            vs_match = re.search(r"\b(?:vs\.?|v\.)\s*$", title, re.IGNORECASE)
            if vs_match and "vs" in title_lower:
                assert False, (
                    f"Case title ends with 'vs' and no defendant: "
                    f"{title!r} (case_number={r.case_number})"
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

    def test_case_titles_are_clean(self, west_text: str) -> None:
        """Case titles should be clean names, not garbled with ruling text (#1357)."""
        results = _split_case_number_based(west_text)
        ruling_keywords = ["granted", "denied", "relief"]
        for r in results:
            title = r.case_title or ""
            title_lower = title.lower()
            for kw in ruling_keywords:
                assert kw not in title_lower, (
                    f"Case title contains ruling text keyword {kw!r}: "
                    f"{title!r} (case_number={r.case_number})"
                )
            assert len(title) <= 80, (
                f"Case title too long (likely garbled): "
                f"{title!r} (len={len(title)}, case_number={r.case_number})"
            )
            # Titles containing 'vs' should have a non-empty defendant name
            vs_match = re.search(r"\b(?:vs\.?|v\.)\s*$", title, re.IGNORECASE)
            if vs_match and "vs" in title_lower:
                assert False, (
                    f"Case title ends with 'vs' and no defendant: "
                    f"{title!r} (case_number={r.case_number})"
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

    def test_case_titles_are_clean(self, cm_text: str) -> None:
        """Case titles should be clean names, not garbled with ruling text (#1357)."""
        results = _split_case_number_based(cm_text)
        ruling_keywords = ["granted", "denied", "relief"]
        for r in results:
            title = r.case_title or ""
            title_lower = title.lower()
            for kw in ruling_keywords:
                assert kw not in title_lower, (
                    f"Case title contains ruling text keyword {kw!r}: "
                    f"{title!r} (case_number={r.case_number})"
                )
            assert len(title) <= 80, (
                f"Case title too long (likely garbled): "
                f"{title!r} (len={len(title)}, case_number={r.case_number})"
            )
            # Titles containing 'vs' should have a non-empty defendant name
            vs_match = re.search(r"\b(?:vs\.?|v\.)\s*$", title, re.IGNORECASE)
            if vs_match and "vs" in title_lower:
                assert False, (
                    f"Case title ends with 'vs' and no defendant: "
                    f"{title!r} (case_number={r.case_number})"
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

    def test_case_titles_are_clean(self, cx_text: str) -> None:
        """Case titles should be clean names, not garbled with ruling text (#1357)."""
        results = _split_case_number_based(cx_text)
        ruling_keywords = ["granted", "denied", "relief"]
        for r in results:
            title = r.case_title or ""
            title_lower = title.lower()
            for kw in ruling_keywords:
                assert kw not in title_lower, (
                    f"Case title contains ruling text keyword {kw!r}: "
                    f"{title!r} (case_number={r.case_number})"
                )
            assert len(title) <= 80, (
                f"Case title too long (likely garbled): "
                f"{title!r} (len={len(title)}, case_number={r.case_number})"
            )
            # Titles containing 'vs' should have a non-empty defendant name
            vs_match = re.search(r"\b(?:vs\.?|v\.)\s*$", title, re.IGNORECASE)
            if vs_match and "vs" in title_lower:
                assert False, (
                    f"Case title ends with 'vs' and no defendant: "
                    f"{title!r} (case_number={r.case_number})"
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
        # Single entry means no splitting — pass through with original text
        assert len(results) == 1
        assert results[0].ruling_text == text

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


# ---------------------------------------------------------------------------
# Boilerplate-only filter tests (#1253)
# ---------------------------------------------------------------------------


class TestIsBoilerplateOnlyFilter:
    """Tests for the _is_boilerplate_only() filter function (#1253).

    The OC splitter sometimes produces split results that contain only
    the calendar header boilerplate without any case-specific ruling text.
    These boilerplate-only results should be filtered out.
    """

    def test_short_header_only_text_is_boilerplate(self) -> None:
        """Text with only 'TENTATIVE RULINGS' header content is boilerplate."""
        text = (
            "TENTATIVE RULINGS\n"
            "LAW & MOTION CALENDAR\n"
            "Judge Deborah C. Servino\n"
            "DEPT N15\n"
            "Date: March 16, 2026\n"
            "Time: 2:00 PM\n"
            "If you are submitting to the tentative, please call the Clerk.\n"
            "#  Case Name  Tentative\n"
        )
        assert _is_boilerplate_only(text) is True

    def test_substantive_ruling_is_not_boilerplate(self) -> None:
        """Text with actual ruling content is not boilerplate."""
        text = (
            "1 Avila vs. Southern California Motion for Summary Judgment\n"
            "The Court has considered the moving, opposing, and reply papers.\n"
            "The motion for summary judgment is DENIED.\n"
            "Defendant has not met its burden of showing that there is no\n"
            "triable issue of material fact. The Court finds that plaintiff\n"
            "has raised a triable issue as to whether defendant was negligent\n"
            "in its maintenance of the rail crossing.\n"
            "Moving party to give notice.\n"
        )
        assert _is_boilerplate_only(text) is False

    def test_long_header_text_is_boilerplate(self) -> None:
        """Even slightly longer header text without case content is boilerplate."""
        text = (
            "TENTATIVE RULINGS\n"
            "LAW & MOTION CALENDAR\n"
            "Judge Deborah C. Servino\n"
            "DEPT N15\n"
            "Date: March 16, 2026\n"
            "Time: 2:00 PM\n"
            "North Justice Center\n"
            "If you are submitting to the tentative, please call the\n"
            "Clerk at (657) 622-5298.\n"
            "Any party may request oral argument.\n"
            "#  Case Name  Tentative\n"
        )
        assert _is_boilerplate_only(text) is True

    def test_short_but_substantive_ruling_is_not_boilerplate(self) -> None:
        """A short ruling with actual case content is not boilerplate."""
        text = (
            "1 Smith vs. Jones Demurrer\nThe demurrer is SUSTAINED with 10 days leave to amend.\n"
        )
        assert _is_boilerplate_only(text) is False

    def test_empty_text_is_boilerplate(self) -> None:
        """Empty text is treated as boilerplate."""
        assert _is_boilerplate_only("") is True

    def test_whitespace_only_is_boilerplate(self) -> None:
        """Whitespace-only text is boilerplate."""
        assert _is_boilerplate_only("   \n  \n  ") is True

    def test_header_with_case_content_below_is_not_boilerplate(self) -> None:
        """Header + actual ruling content is not boilerplate."""
        text = (
            "TENTATIVE RULINGS\n"
            "LAW & MOTION CALENDAR\n"
            "Judge Deborah C. Servino\n"
            "1 Avila vs. Southern California Motion for Summary Judgment\n"
            "The motion is DENIED. Defendant has not met its burden.\n"
            "Moving party to give notice.\n"
        )
        assert _is_boilerplate_only(text) is False

    def test_ruling_with_boilerplate_keyword_is_not_boilerplate(self) -> None:
        """A ruling containing 'oral argument' is NOT boilerplate if it has outcome."""
        text = (
            "TENTATIVE RULINGS\n"
            "The request for oral argument is denied; the motion is taken under submission.\n"
        )
        # Even though this contains "TENTATIVE RULINGS" and "oral argument",
        # the second line also contains "denied" (a ruling keyword), so it
        # should be counted as substantive.
        assert _is_boilerplate_only(text) is False

    def test_avila_boilerplate_example(self) -> None:
        """The specific Avila boilerplate example from issue #1253."""
        # ~598 chars of header only, as described in the issue
        text = (
            "TENTATIVE RULINGS\n"
            "LAW & MOTION CALENDAR\n"
            "Judge Deborah C. Servino\n"
            "DEPT N15\n"
            "Date: March 16, 2026\n"
            "Time: 2:00 PM\n"
            "North Justice Center\n"
            "1141 N. Rice Street\n"
            "Anaheim, CA 92801\n"
            "If you are submitting to the tentative, please call the\n"
            "Clerk at (657) 622-5298 by 3:30 p.m.\n"
            "Any party may request oral argument.\n"
            "If ALL parties submit to the tentative, oral argument will\n"
            "not be scheduled.\n"
            "#  Case Name  Tentative\n"
        )
        assert _is_boilerplate_only(text) is True


class TestBoilerplateFilterIntegration:
    """Tests that boilerplate-only results are filtered out of split results (#1253)."""

    def test_north_split_filters_boilerplate_header(self) -> None:
        """When a North split produces a boilerplate-only entry, it is removed."""
        # Simulate a document where the header area might create a false
        # boundary before the real case entries.
        text = (
            "TENTATIVE RULINGS\n"
            "LAW & MOTION CALENDAR\n"
            "Judge Deborah C. Servino\n"
            "DEPT N15\n"
            "Date: March 16, 2026\n"
            "#\n"
            "1 Avila vs. Southern California Motion for Summary Judgment\n"
            "The motion is DENIED.\n"
            "Defendant has not met its burden of showing no triable issue.\n"
            "2 Smith vs. Jones Demurrer to Complaint\n"
            "The demurrer is SUSTAINED with 10 days leave to amend.\n"
        )
        event = {"ruling_text": text, "department": "N15"}
        results = split_oc_document(event)
        # Should have 2 results, not 3 (no boilerplate-only entry)
        assert len(results) == 2
        for r in results:
            assert not _is_boilerplate_only(r.ruling_text)

    def test_case_number_split_filters_boilerplate_header(self) -> None:
        """When a case-number split produces a boilerplate header, it is removed."""
        text = (
            "TENTATIVE RULINGS\n"
            "LAW & MOTION CALENDAR\n"
            "DEPT C25\n"
            "Date: March 16, 2026\n"
            "#  Case Name  Tentative\n"
            "1. Smith vs. Jones Motion for Summary Judgment\n"
            "25-01455183\n"
            "The motion is GRANTED in its entirety.\n"
            "Defendant shall prepare a proposed order.\n"
            "2. Doe vs. Roe Demurrer to Complaint\n"
            "24-01428812\n"
            "The demurrer is OVERRULED.\n"
        )
        event = {"ruling_text": text, "department": "C25"}
        results = split_oc_document(event)
        # All results should have substantive content
        for r in results:
            assert not _is_boilerplate_only(r.ruling_text)

    def test_all_boilerplate_returns_empty(self) -> None:
        """If all split results are boilerplate, return empty (triggers pass-through)."""
        # This is a degenerate case — shouldn't happen in practice but we
        # should handle it gracefully.
        text = "TENTATIVE RULINGS\nLAW & MOTION CALENDAR\nDEPT N15\n"
        event = {"ruling_text": text, "department": "N15"}
        results = split_oc_document(event)
        # _split_north returns empty for < 2 entries, so split_oc_document
        # falls through to _split_case_number_based which also returns empty.
        # The framework's split_document() then passes through unchanged.
        assert len(results) == 0 or all(not _is_boilerplate_only(r.ruling_text) for r in results)

    def test_filter_preserves_legitimate_short_rulings(self) -> None:
        """Short rulings with actual case content are NOT filtered out."""
        text = (
            "1 Alpha vs. Beta Demurrer\n"
            "The demurrer is SUSTAINED.\n"
            "2 Gamma vs. Delta OSC\n"
            "Continued to April 15, 2026.\n"
        )
        event = {"ruling_text": text, "department": "N15"}
        results = split_oc_document(event)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Real North JC fixture regression tests — per-department (#1234)
# ---------------------------------------------------------------------------


class TestNorthN15Fixture:
    """Regression tests using the real N15 (Judge Nathan Vu) PDF fixture."""

    @pytest.fixture()
    def n15_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_north_n15.pdf"))

    def test_splits_multiple_cases(self, n15_text: str) -> None:
        results = _split_north(n15_text)
        assert len(results) >= 8

    def test_all_entries_have_case_title_with_vs(self, n15_text: str) -> None:
        results = _split_north(n15_text)
        for r in results:
            assert r.case_title is not None
            assert re.search(r"\b(?:vs\.?|v\.)", r.case_title, re.IGNORECASE), (
                f"Entry missing 'vs' in title: {r.case_title}"
            )

    def test_thomson_entry(self, n15_text: str) -> None:
        """N15 contains a Thomson vs. Toyota Motor demurrer."""
        results = _split_north(n15_text)
        thomson = [r for r in results if "Thomson" in (r.case_title or "")]
        assert len(thomson) == 1
        assert thomson[0].motion_type is not None
        assert "Demurrer" in thomson[0].motion_type

    def test_each_entry_has_unique_ruling_text(self, n15_text: str) -> None:
        results = _split_north(n15_text)
        texts = [r.ruling_text for r in results]
        assert len(set(texts)) == len(texts)

    def test_case_titles_are_clean(self, n15_text: str) -> None:
        """Case titles should be clean names, not garbled with ruling text (#1357)."""
        results = _split_north(n15_text)
        ruling_keywords = ["granted", "denied", "relief"]
        for r in results:
            title = r.case_title or ""
            title_lower = title.lower()
            for kw in ruling_keywords:
                assert kw not in title_lower, (
                    f"Case title contains ruling text keyword {kw!r}: "
                    f"{title!r} (case_number={r.case_number})"
                )
            assert len(title) <= 80, (
                f"Case title too long (likely garbled): "
                f"{title!r} (len={len(title)}, case_number={r.case_number})"
            )
            # Titles containing 'vs' should have a non-empty defendant name
            vs_match = re.search(r"\b(?:vs\.?|v\.)\s*$", title, re.IGNORECASE)
            if vs_match and "vs" in title_lower:
                assert False, (
                    f"Case title ends with 'vs' and no defendant: "
                    f"{title!r} (case_number={r.case_number})"
                )

    def test_split_text_shorter_than_full_page(self, n15_text: str) -> None:
        results = _split_north(n15_text)
        full_len = len(n15_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len, (
                f"Split [{i}] has {len(r.ruling_text)} chars, same as full page ({full_len})."
            )

    def test_split_via_framework(self, n15_text: str) -> None:
        event = {
            "state": "CA",
            "county": "Orange",
            "ruling_text": n15_text,
            "department": "N15",
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) >= 8


class TestNorthN16Fixture:
    """Regression tests using the real N16 (Judge Donald Gaffney) PDF fixture."""

    @pytest.fixture()
    def n16_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_north_n16.pdf"))

    def test_splits_multiple_cases(self, n16_text: str) -> None:
        results = _split_north(n16_text)
        assert len(results) >= 7

    def test_all_entries_have_case_title_with_vs(self, n16_text: str) -> None:
        results = _split_north(n16_text)
        for r in results:
            assert r.case_title is not None
            assert re.search(r"\b(?:vs\.?|v\.)", r.case_title, re.IGNORECASE), (
                f"Entry missing 'vs' in title: {r.case_title}"
            )

    def test_gordon_entry(self, n16_text: str) -> None:
        """N16 contains a Gordon vs. Volvo entry."""
        results = _split_north(n16_text)
        gordon = [r for r in results if "Gordon" in (r.case_title or "")]
        assert len(gordon) == 1

    def test_each_entry_has_unique_ruling_text(self, n16_text: str) -> None:
        results = _split_north(n16_text)
        texts = [r.ruling_text for r in results]
        assert len(set(texts)) == len(texts)

    def test_case_titles_are_clean(self, n16_text: str) -> None:
        """Case titles should be clean names, not garbled with ruling text (#1357)."""
        results = _split_north(n16_text)
        ruling_keywords = ["granted", "denied", "relief"]
        for r in results:
            title = r.case_title or ""
            title_lower = title.lower()
            for kw in ruling_keywords:
                assert kw not in title_lower, (
                    f"Case title contains ruling text keyword {kw!r}: "
                    f"{title!r} (case_number={r.case_number})"
                )
            assert len(title) <= 80, (
                f"Case title too long (likely garbled): "
                f"{title!r} (len={len(title)}, case_number={r.case_number})"
            )
            # Titles containing 'vs' should have a non-empty defendant name
            vs_match = re.search(r"\b(?:vs\.?|v\.)\s*$", title, re.IGNORECASE)
            if vs_match and "vs" in title_lower:
                assert False, (
                    f"Case title ends with 'vs' and no defendant: "
                    f"{title!r} (case_number={r.case_number})"
                )

    def test_split_text_shorter_than_full_page(self, n16_text: str) -> None:
        results = _split_north(n16_text)
        full_len = len(n16_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len, (
                f"Split [{i}] has {len(r.ruling_text)} chars, same as full page ({full_len})."
            )

    def test_split_via_framework(self, n16_text: str) -> None:
        event = {
            "state": "CA",
            "county": "Orange",
            "ruling_text": n16_text,
            "department": "N16",
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) >= 7


class TestNorthN17Fixture:
    """Regression tests using the real N17 (Judge Craig L. Griffin) PDF fixture."""

    @pytest.fixture()
    def n17_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_north_n17.pdf"))

    def test_splits_multiple_cases(self, n17_text: str) -> None:
        results = _split_north(n17_text)
        assert len(results) >= 5

    def test_all_entries_have_case_title_with_vs(self, n17_text: str) -> None:
        results = _split_north(n17_text)
        for r in results:
            assert r.case_title is not None
            assert re.search(r"\b(?:vs\.?|v\.)", r.case_title, re.IGNORECASE), (
                f"Entry missing 'vs' in title: {r.case_title}"
            )

    def test_zavala_entry(self, n17_text: str) -> None:
        """N17 contains a Zavala vs. Becker entry (the case from #1093)."""
        results = _split_north(n17_text)
        zavala = [r for r in results if "Zavala" in (r.case_title or "")]
        assert len(zavala) == 1
        assert zavala[0].motion_type is not None
        assert "Attorneys' Fees" in zavala[0].motion_type

    def test_post_v_chung_entry(self, n17_text: str) -> None:
        """N17 contains a Post v. Chung entry using 'v.' separator."""
        results = _split_north(n17_text)
        post = [r for r in results if "Post" in (r.case_title or "")]
        assert len(post) == 1

    def test_each_entry_has_unique_ruling_text(self, n17_text: str) -> None:
        results = _split_north(n17_text)
        texts = [r.ruling_text for r in results]
        assert len(set(texts)) == len(texts)

    def test_case_titles_are_clean(self, n17_text: str) -> None:
        """Case titles should be clean names, not garbled with ruling text (#1357)."""
        results = _split_north(n17_text)
        ruling_keywords = ["granted", "denied", "relief"]
        for r in results:
            title = r.case_title or ""
            title_lower = title.lower()
            for kw in ruling_keywords:
                assert kw not in title_lower, (
                    f"Case title contains ruling text keyword {kw!r}: "
                    f"{title!r} (case_number={r.case_number})"
                )
            assert len(title) <= 80, (
                f"Case title too long (likely garbled): "
                f"{title!r} (len={len(title)}, case_number={r.case_number})"
            )
            # Titles containing 'vs' should have a non-empty defendant name
            vs_match = re.search(r"\b(?:vs\.?|v\.)\s*$", title, re.IGNORECASE)
            if vs_match and "vs" in title_lower:
                assert False, (
                    f"Case title ends with 'vs' and no defendant: "
                    f"{title!r} (case_number={r.case_number})"
                )

    def test_split_text_shorter_than_full_page(self, n17_text: str) -> None:
        results = _split_north(n17_text)
        full_len = len(n17_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len, (
                f"Split [{i}] has {len(r.ruling_text)} chars, same as full page ({full_len})."
            )

    def test_split_via_framework(self, n17_text: str) -> None:
        event = {
            "state": "CA",
            "county": "Orange",
            "ruling_text": n17_text,
            "department": "N17",
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) >= 5


class TestNorthN18Fixture:
    """Regression tests using the real N18 (Judge Scott A. Steiner) PDF fixture.

    N18 uses a different format from other North JC departments: it includes
    case numbers (e.g. '2024-1389826') and numbered entries with case names
    on continuation lines. This means the North splitter finds few or no entries,
    and the document is split by the case-number-based fallback path.
    """

    @pytest.fixture()
    def n18_text(self) -> str:
        return _extract_pdf_text(_load_bytes("oc_north_n18.pdf"))

    def test_case_number_splitter_finds_entries(self, n18_text: str) -> None:
        """The case-number-based splitter handles N18's format."""
        results = _split_case_number_based(n18_text)
        assert len(results) >= 8

    def test_all_entries_have_case_numbers(self, n18_text: str) -> None:
        results = _split_case_number_based(n18_text)
        for r in results:
            assert r.case_number is not None, (
                f"Entry missing case number. Title: {r.case_title}, "
                f"Text start: {r.ruling_text[:80]}"
            )

    def test_suarez_entry(self, n18_text: str) -> None:
        """N18 contains a Suarez vs. Lopez entry."""
        results = _split_case_number_based(n18_text)
        suarez = [r for r in results if "Suarez" in (r.case_title or "")]
        assert len(suarez) == 1
        assert suarez[0].case_number == "2024-1389826"

    def test_guo_entry(self, n18_text: str) -> None:
        """N18 contains a Guo vs. Zhang entry."""
        results = _split_case_number_based(n18_text)
        guo = [r for r in results if "Guo" in (r.case_title or "")]
        assert len(guo) == 1
        assert guo[0].case_number == "2023-1356203"

    def test_each_entry_has_unique_ruling_text(self, n18_text: str) -> None:
        results = _split_case_number_based(n18_text)
        texts = [r.ruling_text for r in results]
        assert len(set(texts)) == len(texts)

    def test_split_text_shorter_than_full_page(self, n18_text: str) -> None:
        results = _split_case_number_based(n18_text)
        full_len = len(n18_text)
        for i, r in enumerate(results):
            assert len(r.ruling_text) < full_len * 0.5, (
                f"Split [{i}] (case={r.case_number}) has {len(r.ruling_text)} chars, "
                f">= 50% of full page ({full_len})."
            )

    def test_north_splitter_falls_back_to_case_number(self, n18_text: str) -> None:
        """N18 uses case numbers, so split_oc_document falls through to the
        case-number-based splitter (#1232).

        The North splitter returns empty for N18 because it finds fewer than 2
        entries matching the "vs" pattern (any stray legal citations like
        'Mercuro v. Superior Court' are filtered by the < 2 minimum).  The
        framework then falls back to _split_case_number_based which correctly
        handles N18's format (period after entry number, case number prefix).
        """
        # North splitter returns empty for N18 (< 2 valid entries)
        north_results = _split_north(n18_text)
        assert len(north_results) == 0

        # Case-number-based splitter handles N18 correctly
        cn_results = _split_case_number_based(n18_text)
        assert len(cn_results) >= 8

        # Framework dispatch falls through to case-number-based splitting
        event = {"ruling_text": n18_text, "department": "N18"}
        fw_results = split_oc_document(event)
        assert len(fw_results) >= 8
        # All entries should have case numbers (N18 includes them)
        for r in fw_results:
            assert r.case_number is not None

    def test_split_via_framework(self, n18_text: str) -> None:
        """Verify the full framework pipeline works for N18."""
        event = {
            "state": "CA",
            "county": "Orange",
            "ruling_text": n18_text,
            "department": "N18",
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) >= 8

    def test_n18_case_titles_are_clean(self, n18_text: str) -> None:
        """N18 case titles should be clean 'X vs Y' format, not garbled (#1331).

        Before the fix, the two-column PDF layout caused ruling text to leak
        into case titles (e.g. "Suarez vs. seeking relief from the Order...").
        After the fix, titles should be clean names like "Suarez vs Lopez".
        """
        results = _split_case_number_based(n18_text)
        for r in results:
            title = r.case_title or ""
            # Titles should not contain ruling-text keywords.
            assert "granted" not in title.lower(), (
                f"Case title contains ruling text: {title!r} (case_number={r.case_number})"
            )
            assert "denied" not in title.lower(), (
                f"Case title contains ruling text: {title!r} (case_number={r.case_number})"
            )
            assert "motion" not in title.lower(), (
                f"Case title contains ruling text: {title!r} (case_number={r.case_number})"
            )
            # Titles should be reasonably short (not garbled with ruling text).
            assert len(title) < 60, (
                f"Case title too long (likely garbled): {title!r} (case_number={r.case_number})"
            )

    def test_n18_suarez_title_is_clean(self, n18_text: str) -> None:
        """Suarez entry should have a clean title, not garbled ruling text."""
        results = _split_case_number_based(n18_text)
        suarez = [r for r in results if "Suarez" in (r.case_title or "")]
        assert len(suarez) == 1
        title = suarez[0].case_title
        assert title is not None
        assert "Lopez" in title, f"Title should contain 'Lopez': {title!r}"
        assert "seeking" not in title, f"Title has ruling text: {title!r}"
        assert "relief" not in title, f"Title has ruling text: {title!r}"

    def test_n18_zahab_title_is_clean(self, n18_text: str) -> None:
        """Zahab entry should include defendant name Myrick."""
        results = _split_case_number_based(n18_text)
        zahab = [r for r in results if "Zahab" in (r.case_title or "")]
        assert len(zahab) == 1
        title = zahab[0].case_title
        assert title is not None
        assert "Myrick" in title, f"Title should contain 'Myrick': {title!r}"

    def test_n18_kamell_title_is_clean(self, n18_text: str) -> None:
        """Kamell entry should include defendant name Habashi."""
        results = _split_case_number_based(n18_text)
        kamell = [r for r in results if "Kamell" in (r.case_title or "")]
        assert len(kamell) == 1
        title = kamell[0].case_title
        assert title is not None
        assert "Habashi" in title, f"Title should contain 'Habashi': {title!r}"


# ---------------------------------------------------------------------------
# Multi-line case title extraction tests (#1331)
# ---------------------------------------------------------------------------


class TestExtractLeadingNameFragment:
    """Tests for _extract_leading_name_fragment."""

    def test_short_name_fragment(self) -> None:
        assert _extract_leading_name_fragment("Lopez") == "Lopez"

    def test_short_name_with_vs(self) -> None:
        result = _extract_leading_name_fragment("Zahab vs. Case Management Conference")
        assert result is not None
        assert "Zahab vs" in result
        assert "Management" not in result

    def test_vs_with_defendant_name(self) -> None:
        result = _extract_leading_name_fragment("Guo vs. Zhang Case Management Conference")
        assert result is not None
        assert "Zhang" in result
        assert "Management" not in result

    def test_ruling_text_line_returns_none(self) -> None:
        assert _extract_leading_name_fragment("The motion is granted.") is None

    def test_plaintiff_line_returns_none(self) -> None:
        assert _extract_leading_name_fragment("Plaintiff filed a motion to compel.") is None

    def test_defendant_line_returns_none(self) -> None:
        assert _extract_leading_name_fragment("Defendant Bo Zhang's motion to dismiss...") is None

    def test_empty_line_returns_none(self) -> None:
        assert _extract_leading_name_fragment("") is None

    def test_blank_line_returns_none(self) -> None:
        assert _extract_leading_name_fragment("   ") is None


class TestExtractMultilineCaseTitle:
    """Tests for _extract_multiline_case_title."""

    def test_two_line_case_name(self) -> None:
        """Case name split across two continuation lines."""
        lines = [
            "2. 2024-1389826 The motion by Plaintiff Andres Suarez for an order",
            "Suarez vs. seeking relief from the Order of dismissal",
            "Lopez 2025, is granted.",
            "As an initial matter, the Court notes...",
        ]
        result = _extract_multiline_case_title(lines, 0, len(lines))
        assert result is not None
        assert "Suarez" in result
        assert "Lopez" in result
        assert "seeking" not in result

    def test_single_line_with_vs_and_defendant(self) -> None:
        """Case name on a single continuation line with complete vs."""
        lines = [
            "10. 2023-1356203",
            "Guo vs. Zhang Case Management Conference",
            "Defendant Bo Zhang's motion to dismiss...",
        ]
        result = _extract_multiline_case_title(lines, 0, len(lines))
        assert result is not None
        assert "Guo" in result
        assert "Zhang" in result
        assert "Defendant" not in result

    def test_no_vs_returns_none(self) -> None:
        """Lines without 'vs' produce no title."""
        lines = [
            "1. 2024-1234567 Motion for summary judgment",
            "The court grants the motion.",
        ]
        assert _extract_multiline_case_title(lines, 0, len(lines)) is None

    def test_vs_with_separate_defendant_line(self) -> None:
        """Defendant name on a separate short line after the vs line."""
        lines = [
            "12. 2022-1264619",
            "Kamell vs. Case Management Conference",
            "Habashi",
            "Plaintiff Rafik Y. Kamell's Motion...",
        ]
        result = _extract_multiline_case_title(lines, 0, len(lines))
        assert result is not None
        assert "Kamell" in result
        assert "Habashi" in result
        assert "Management" not in result


# ---------------------------------------------------------------------------
# Case title cleaning tests (#1304)
# ---------------------------------------------------------------------------


class TestCleanCaseTitle:
    """Tests for _clean_case_title — stripping ruling text from case titles."""

    def test_strips_is_granted(self) -> None:
        raw = "Gonzalez vs. Attorney\u2019s Fees in the Sum of $1,038,085.00 is GRANTED"
        result = _clean_case_title(raw)
        assert "GRANTED" not in result

    def test_strips_is_denied(self) -> None:
        raw = "Miranda vs. Chiron, Strike is DENIED."
        assert "DENIED" not in _clean_case_title(raw)

    def test_strips_is_sustained_overruled(self) -> None:
        raw = "vs. Azzalino Union\u2019s Complaint is OVERRULED is part and SUSTAINED in part"
        result = _clean_case_title(raw)
        assert "OVERRULED" not in result
        assert "SUSTAINED" not in result

    def test_strips_continued(self) -> None:
        raw = "One LLP vs. CONTINUED TO APRIL 21, 2026, AT 9:00 A.M., IN"
        # "CONTINUED" pattern: the text after "vs." starts with it
        # The cleaning should at least remove the ruling text
        result = _clean_case_title(raw)
        assert "APRIL 21" not in result

    def test_strips_for_attorney_fees(self) -> None:
        raw = "vs. Kelley for Attorney Fees is GRANTED in the reduced amount of $67,746.32."
        result = _clean_case_title(raw)
        assert "GRANTED" not in result
        assert "Attorney Fees" not in result

    def test_strips_approval_of_class_action(self) -> None:
        raw = "Jones vs. Saddle Creek of Class Action and PAGA Settlement is GRANTED IN"
        result = _clean_case_title(raw)
        assert "GRANTED" not in result
        assert "Class Action" not in result

    def test_strips_possessive_plaintiff(self) -> None:
        raw = "Robles vs. Bally Plaintiff\u2019s"
        result = _clean_case_title(raw)
        assert "Plaintiff" not in result

    def test_strips_possessive_defendant(self) -> None:
        raw = "Keller vs. System Defendant\u2019s"
        result = _clean_case_title(raw)
        assert "Defendant" not in result

    def test_strips_cross_defendant(self) -> None:
        raw = "Desaluna vs. WL Cross- Defendant\u2019s"
        result = _clean_case_title(raw)
        assert "Defendant" not in result

    def test_preserves_clean_title(self) -> None:
        raw = "Gomez vs. Black Lion Farms, LLC"
        assert _clean_case_title(raw) == "Gomez vs. Black Lion Farms, LLC"

    def test_preserves_simple_vs_title(self) -> None:
        raw = "Smith vs. Jones"
        assert _clean_case_title(raw) == "Smith vs. Jones"

    def test_empty_string(self) -> None:
        assert _clean_case_title("") == ""

    def test_strips_settlement_funds(self) -> None:
        raw = "Hoerner vs. Hoag settlement funds has been made in accordance with the"
        result = _clean_case_title(raw)
        assert "settlement funds" not in result

    def test_strips_to_compel(self) -> None:
        raw = "Hallum vs. Restaurant to Compel Arbitration is GRANTED. IT IS ORDERED"
        result = _clean_case_title(raw)
        assert "GRANTED" not in result
        assert "Compel" not in result


class TestExtractCaseTitleFromEntry:
    """Tests for _extract_case_title_from_entry with garbled title cleanup."""

    def test_basic_vs_with_case_number(self) -> None:
        line = "30-2024-01393434 Smith vs. Jones Motion for Summary Judgment"
        result = _extract_case_title_from_entry(line)
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result
        assert "Motion" not in result

    def test_garbled_with_granted(self) -> None:
        line = "30-2024-01393434 Jones vs. Creek of Class Action is GRANTED"
        result = _extract_case_title_from_entry(line)
        assert result is not None
        assert "GRANTED" not in result

    def test_no_vs_returns_none(self) -> None:
        line = "30-2024-01393434 Motion for Summary Judgment"
        result = _extract_case_title_from_entry(line)
        assert result is None

    def test_cleans_possessive_role(self) -> None:
        line = "30-2025-01477814 Keller vs. System Defendant\u2019s"
        result = _extract_case_title_from_entry(line)
        assert result is not None
        assert "Defendant" not in result
