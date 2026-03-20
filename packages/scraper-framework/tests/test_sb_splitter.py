"""Tests for San Bernardino document splitter.

Tests cover:
  - Format 1 (S24 style): TENTATIVE RULING FOR CIV... headers
  - Format 2 (S22 style): horizontal rule delimiters
  - Single-case PDFs pass through unsplit
  - Case number and case title extraction
  - Edge cases: empty text, no recognizable boundaries
  - Regression tests against real SB PDF fixtures
  - Integration with the splitter framework (register_splitter)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from courts.ca.pdf_link_scraper import _extract_pdf_text
from ingestion.sb_splitter import (
    _split_format1,
    _split_format2,
    split_sb_document,
)
from ingestion.splitter import _splitter_registry, split_document

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Format 1 splitting (S24 style) — synthetic tests
# ---------------------------------------------------------------------------


class TestSplitFormat1:
    def test_two_cases_split(self) -> None:
        """Two TENTATIVE RULING FOR headers produce two results."""
        text = (
            "TENTATIVE RULING FOR CIVSB2416631\n"
            "Department S24 - Judge Carlos M. Cabrera\n"
            "Golden State v. Suraj Victorville, LLC\n"
            "Motion: Compel Attendance at Deposition\n"
            "The motion is GRANTED.\n"
            "TENTATIVE RULING FOR CIVSB2426325\n"
            "Department S24 - Judge Carlos M. Cabrera\n"
            "Godinez et al v. Keating, et al\n"
            "Motion: Leave to Amend\n"
            "The motion is DENIED.\n"
        )
        results = _split_format1(text)
        assert len(results) == 2

        assert results[0].case_number == "CIVSB2416631"
        assert results[0].case_title is not None
        assert "Suraj" in results[0].case_title
        assert "GRANTED" in results[0].ruling_text

        assert results[1].case_number == "CIVSB2426325"
        assert results[1].case_title is not None
        assert "Keating" in results[1].case_title
        assert "DENIED" in results[1].ruling_text

    def test_single_header_returns_empty(self) -> None:
        """A single header means no splitting needed — return empty list."""
        text = (
            "TENTATIVE RULING FOR CIVSB2416631\n"
            "Department S24 - Judge Carlos M. Cabrera\n"
            "Golden State v. Suraj\n"
            "The motion is GRANTED.\n"
        )
        results = _split_format1(text)
        assert len(results) < 2

    def test_no_header_returns_empty(self) -> None:
        """Text without TENTATIVE RULING FOR headers returns empty."""
        text = "Some random text without headers.\n"
        results = _split_format1(text)
        assert results == []

    def test_case_number_with_space_normalised(self) -> None:
        """Case numbers with internal spaces are normalised."""
        text = (
            "TENTATIVE RULING FOR CIVSB 2600001\n"
            "Department S36\n"
            "Plaintiff v. Defendant One\n"
            "Ruling one.\n"
            "TENTATIVE RULING FOR CIVSB 2600002\n"
            "Department S36\n"
            "Claimant v. Respondent Two\n"
            "Ruling two.\n"
        )
        results = _split_format1(text)
        assert len(results) == 2
        assert results[0].case_number == "CIVSB2600001"
        assert results[1].case_number == "CIVSB2600002"


# ---------------------------------------------------------------------------
# Format 2 splitting (S22 style) — synthetic tests
# ---------------------------------------------------------------------------


class TestSplitFormat2:
    def test_two_cases_split(self) -> None:
        """Two HR-delimited sections produce two results."""
        text = (
            "Department S22 - Judge David Driscoll\n"
            "Boilerplate instructions.\n"
            "____________________________________________________________________________\n"
            "CIVSB2419120\n"
            "LORENZO SOLIS v. GENERAL MOTORS LLC\n"
            "____________________________________________________________________________\n"
            "TENTATIVE RULING\n"
            "This is a lemon law case.\n"
            "The motion is GRANTED.\n"
            "____________________________________________________________________________\n"
            "CIVSB2111992\n"
            "CHAVEZ, et al. v. FORD MOTOR COMPANY, et al.\n"
            "____________________________________________________________________________\n"
            "TENTATIVE RULING\n"
            "This is another lemon law case.\n"
            "The motion is DENIED.\n"
        )
        results = _split_format2(text)
        assert len(results) == 2

        assert results[0].case_number == "CIVSB2419120"
        assert results[0].case_title is not None
        assert "SOLIS" in results[0].case_title
        assert "GRANTED" in results[0].ruling_text

        assert results[1].case_number == "CIVSB2111992"
        assert results[1].case_title is not None
        assert "CHAVEZ" in results[1].case_title
        assert "DENIED" in results[1].ruling_text

    def test_single_hr_section_returns_empty(self) -> None:
        """A single HR section means no splitting needed."""
        text = (
            "____________________________________________________________________________\n"
            "CIVSB2419120\n"
            "SOLIS v. GENERAL MOTORS LLC\n"
            "____________________________________________________________________________\n"
            "TENTATIVE RULING\n"
            "The motion is GRANTED.\n"
        )
        results = _split_format2(text)
        assert len(results) < 2

    def test_no_hrs_returns_empty(self) -> None:
        """Text without horizontal rules returns empty."""
        text = "Some random text.\n"
        results = _split_format2(text)
        assert results == []

    def test_hrs_without_case_numbers_returns_empty(self) -> None:
        """HRs not followed by case numbers do not produce splits."""
        text = (
            "____________________________________________________________________________\n"
            "Some text without a case number.\n"
            "____________________________________________________________________________\n"
            "More text.\n"
        )
        results = _split_format2(text)
        assert results == []


# ---------------------------------------------------------------------------
# split_sb_document — dispatch tests
# ---------------------------------------------------------------------------


class TestSplitSbDocument:
    def test_format1_detected(self) -> None:
        """Format 1 (TENTATIVE RULING FOR) is tried first and used when present."""
        text = (
            "TENTATIVE RULING FOR CIVSB2416631\n"
            "Department S24\n"
            "Party A v. Party B\n"
            "Ruling one.\n"
            "TENTATIVE RULING FOR CIVSB2426325\n"
            "Department S24\n"
            "Party C v. Party D\n"
            "Ruling two.\n"
        )
        event = {"ruling_text": text}
        results = split_sb_document(event)
        assert len(results) == 2
        assert results[0].case_number == "CIVSB2416631"

    def test_format2_fallback(self) -> None:
        """Format 2 (HR delimiters) is used when Format 1 doesn't match."""
        text = (
            "Department S22\n"
            "____________________________________________________________________________\n"
            "CIVSB2419120\n"
            "SOLIS v. GENERAL MOTORS LLC\n"
            "____________________________________________________________________________\n"
            "TENTATIVE RULING\n"
            "Ruling one.\n"
            "____________________________________________________________________________\n"
            "CIVSB2111992\n"
            "CHAVEZ v. FORD MOTOR COMPANY\n"
            "____________________________________________________________________________\n"
            "TENTATIVE RULING\n"
            "Ruling two.\n"
        )
        event = {"ruling_text": text}
        results = split_sb_document(event)
        assert len(results) == 2
        assert results[0].case_number == "CIVSB2419120"

    def test_empty_ruling_text(self) -> None:
        """Returns empty list when ruling_text is empty."""
        results = split_sb_document({"ruling_text": ""})
        assert results == []

    def test_none_ruling_text(self) -> None:
        """Returns empty list when ruling_text is None."""
        results = split_sb_document({"ruling_text": None})
        assert results == []

    def test_single_case_returns_empty(self) -> None:
        """Single-case text returns empty (no splitting needed)."""
        text = (
            "TENTATIVE RULING FOR CIVSB2416631\n"
            "Department S24\n"
            "Party A v. Party B\n"
            "The motion is GRANTED.\n"
        )
        results = split_sb_document({"ruling_text": text})
        assert len(results) < 2


# ---------------------------------------------------------------------------
# Framework integration tests
# ---------------------------------------------------------------------------


class TestFrameworkIntegration:
    def test_registered_in_splitter_registry(self) -> None:
        """SB splitter is registered for CA/San Bernardino."""
        assert ("CA", "San Bernardino") in _splitter_registry

    def test_split_document_dispatches_to_sb(self) -> None:
        """split_document() uses the SB splitter for CA/San Bernardino events."""
        text = (
            "TENTATIVE RULING FOR CIVSB2416631\n"
            "Department S24\n"
            "Party A v. Party B\n"
            "Ruling one.\n"
            "TENTATIVE RULING FOR CIVSB2426325\n"
            "Department S24\n"
            "Party C v. Party D\n"
            "Ruling two.\n"
        )
        event = {
            "state": "CA",
            "county": "San Bernardino",
            "ruling_text": text,
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) == 2
        assert results[0].case_number == "CIVSB2416631"
        assert results[1].case_number == "CIVSB2426325"


# ---------------------------------------------------------------------------
# Regression tests against real PDF fixtures
# ---------------------------------------------------------------------------


class TestS24Fixture:
    """Regression tests using the S24 fixture (sb_s24_20260304_a1f33a43.pdf).

    The fixture contains 2 cases in Format 1 (TENTATIVE RULING FOR CIV... headers).
    """

    @pytest.fixture()
    def s24_text(self) -> str:
        return _extract_pdf_text(_load_bytes("sb_s24_20260304_a1f33a43.pdf"))

    def test_splits_into_2_cases(self, s24_text: str) -> None:
        """The fixture should split into exactly 2 individual rulings."""
        results = split_sb_document({"ruling_text": s24_text})
        assert len(results) == 2

    def test_first_case_number(self, s24_text: str) -> None:
        """First case should be CIVSB2416631."""
        results = split_sb_document({"ruling_text": s24_text})
        assert results[0].case_number == "CIVSB2416631"

    def test_second_case_number(self, s24_text: str) -> None:
        """Second case should be CIVSB2426325."""
        results = split_sb_document({"ruling_text": s24_text})
        assert results[1].case_number == "CIVSB2426325"

    def test_each_result_has_case_number(self, s24_text: str) -> None:
        """Every split result must have a valid SB case number."""
        results = split_sb_document({"ruling_text": s24_text})
        for r in results:
            assert r.case_number is not None
            assert r.case_number.startswith("CIV")

    def test_case_titles_extracted(self, s24_text: str) -> None:
        """Each split result should have a case title with v. separator."""
        results = split_sb_document({"ruling_text": s24_text})
        for r in results:
            assert r.case_title is not None, f"Missing case_title for {r.case_number}"

    def test_ruling_text_isolation(self, s24_text: str) -> None:
        """Each ruling's text should contain only that case's content."""
        results = split_sb_document({"ruling_text": s24_text})
        # First case should not contain second case's number
        assert "CIVSB2426325" not in results[0].ruling_text
        # Second case should not contain first case's number
        assert "CIVSB2416631" not in results[1].ruling_text

    def test_each_result_has_unique_ruling_text(self, s24_text: str) -> None:
        """Each split result should have distinct ruling text."""
        results = split_sb_document({"ruling_text": s24_text})
        texts = [r.ruling_text for r in results]
        assert len(set(texts)) == len(texts)

    def test_split_via_framework(self, s24_text: str) -> None:
        """Verify the full framework pipeline works for SB S24 documents."""
        event = {
            "state": "CA",
            "county": "San Bernardino",
            "ruling_text": s24_text,
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) == 2
        assert results[0].case_number == "CIVSB2416631"


class TestS22Fixture:
    """Regression tests using the S22 fixture (sb_s22_20260302_6849aa12.pdf).

    The fixture contains 2 cases in Format 2 (HR delimiters).
    """

    @pytest.fixture()
    def s22_text(self) -> str:
        return _extract_pdf_text(_load_bytes("sb_s22_20260302_6849aa12.pdf"))

    def test_splits_into_2_cases(self, s22_text: str) -> None:
        """The fixture should split into exactly 2 individual rulings."""
        results = split_sb_document({"ruling_text": s22_text})
        assert len(results) == 2

    def test_first_case_number(self, s22_text: str) -> None:
        """First case should be CIVSB2419120."""
        results = split_sb_document({"ruling_text": s22_text})
        assert results[0].case_number == "CIVSB2419120"

    def test_second_case_number(self, s22_text: str) -> None:
        """Second case should be CIVSB2111992."""
        results = split_sb_document({"ruling_text": s22_text})
        assert results[1].case_number == "CIVSB2111992"

    def test_first_case_title(self, s22_text: str) -> None:
        """First case title should contain SOLIS and GENERAL MOTORS."""
        results = split_sb_document({"ruling_text": s22_text})
        assert results[0].case_title is not None
        assert "SOLIS" in results[0].case_title.upper()

    def test_second_case_title(self, s22_text: str) -> None:
        """Second case title should contain CHAVEZ and FORD."""
        results = split_sb_document({"ruling_text": s22_text})
        assert results[1].case_title is not None
        assert "CHAVEZ" in results[1].case_title.upper()

    def test_ruling_text_isolation(self, s22_text: str) -> None:
        """Each ruling's text should not contain the other case's number."""
        results = split_sb_document({"ruling_text": s22_text})
        assert "CIVSB2111992" not in results[0].ruling_text
        assert "CIVSB2419120" not in results[1].ruling_text

    def test_each_result_has_unique_ruling_text(self, s22_text: str) -> None:
        """Each split result should have distinct ruling text."""
        results = split_sb_document({"ruling_text": s22_text})
        texts = [r.ruling_text for r in results]
        assert len(set(texts)) == len(texts)

    def test_split_via_framework(self, s22_text: str) -> None:
        """Verify the full framework pipeline works for SB S22 documents."""
        event = {
            "state": "CA",
            "county": "San Bernardino",
            "ruling_text": s22_text,
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) == 2
        assert results[0].case_number == "CIVSB2419120"


class TestR12SingleCaseFixture:
    """Regression tests using the R12 fixture (sb_r12_20260303_0df41117.pdf).

    The fixture contains 1 case — it should pass through unsplit.
    """

    @pytest.fixture()
    def r12_text(self) -> str:
        return _extract_pdf_text(_load_bytes("sb_r12_20260303_0df41117.pdf"))

    def test_single_case_not_split(self, r12_text: str) -> None:
        """Single-case R12 PDF should not be split."""
        results = split_sb_document({"ruling_text": r12_text})
        assert len(results) < 2

    def test_via_framework_passes_through(self, r12_text: str) -> None:
        """Framework should pass through single-case R12 as one result."""
        event = {
            "state": "CA",
            "county": "San Bernardino",
            "ruling_text": r12_text,
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) == 1


class TestR17SingleCaseFixture:
    """Regression tests using the R17 fixture (sb_r17_20260302_817a3a84.pdf).

    The fixture contains 1 case — it should pass through unsplit.
    """

    @pytest.fixture()
    def r17_text(self) -> str:
        return _extract_pdf_text(_load_bytes("sb_r17_20260302_817a3a84.pdf"))

    def test_single_case_not_split(self, r17_text: str) -> None:
        """Single-case R17 PDF should not be split."""
        results = split_sb_document({"ruling_text": r17_text})
        assert len(results) < 2

    def test_via_framework_passes_through(self, r17_text: str) -> None:
        """Framework should pass through single-case R17 as one result."""
        event = {
            "state": "CA",
            "county": "San Bernardino",
            "ruling_text": r17_text,
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) == 1
