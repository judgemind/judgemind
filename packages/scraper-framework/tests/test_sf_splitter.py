"""Tests for San Francisco document splitter.

Tests cover:
  - Splitting multi-case SF calendar PDFs into individual rulings
  - Boilerplate page discarding (pages without case numbers)
  - Case number, case title, and motion type extraction
  - Regression tests against the real sf_dept403_ruling.pdf fixture
  - Integration with the splitter framework (register_splitter)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from courts.ca.pdf_link_scraper import _extract_pdf_text
from ingestion.sf_splitter import (
    _extract_motion_type,
    split_sf_document,
)
from ingestion.splitter import _splitter_registry, split_document

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Motion type extraction — unit tests
# ---------------------------------------------------------------------------


class TestExtractMotionType:
    def test_simple_motion_type(self) -> None:
        """Single-line motion type is extracted correctly."""
        chunk = (
            "10 Respondent ) Presiding: BOBBY P. LUNA\n"
            ")\n"
            "11 )\n"
            "12 REQUEST FOR ORDER FOR CHILD CUSTODY\n"
            "13 TENTATIVE RULING\n"
            "The motion is GRANTED.\n"
        )
        result = _extract_motion_type(chunk)
        assert result == "REQUEST FOR ORDER FOR CHILD CUSTODY"

    def test_multiline_motion_type(self) -> None:
        """Multi-line motion type is joined with spaces."""
        chunk = (
            "10 Respondent ) Presiding: BOBBY P. LUNA\n"
            ")\n"
            "11 )\n"
            "12 REQUEST FOR ORDER FOR CHANGE OF CHILD CUSTODY, ATTORNEY'S FEES AND COSTS,\n"
            "13 MOVE-AWAY; PASSPORT FOR CHILD\n"
            "14 TENTATIVE RULING\n"
            "The parties are ordered to appear.\n"
        )
        result = _extract_motion_type(chunk)
        assert result is not None
        assert "REQUEST FOR ORDER" in result
        assert "MOVE-AWAY" in result

    def test_no_respondent_line(self) -> None:
        """Returns None when no Respondent line is present."""
        chunk = "Some text without respondent line\nTENTATIVE RULING\nRuling text.\n"
        result = _extract_motion_type(chunk)
        assert result is None

    def test_no_tentative_ruling_marker(self) -> None:
        """Returns None when TENTATIVE RULING marker is missing."""
        chunk = "10 Respondent ) Presiding: BOBBY P. LUNA\n)\nSome text.\n"
        result = _extract_motion_type(chunk)
        assert result is None

    def test_bare_numbers_stripped(self) -> None:
        """Bare line numbers between motion lines are stripped."""
        chunk = (
            "10 Respondent ) Presiding: JUDGE\n"
            ")\n"
            "11 )\n"
            "12 REQUEST FOR ORDER: SPOUSAL SUPPORT\n"
            "13\n"
            "14 TENTATIVE RULING\n"
            "Ruling here.\n"
        )
        result = _extract_motion_type(chunk)
        assert result == "REQUEST FOR ORDER: SPOUSAL SUPPORT"


# ---------------------------------------------------------------------------
# split_sf_document — synthetic tests
# ---------------------------------------------------------------------------


class TestSplitSfDocument:
    def test_two_cases_split(self) -> None:
        """Two case sections are split into two results."""
        text = (
            "Boilerplate instructions text.\n"
            "1 SUPERIOR COURT OF CALIFORNIA\n"
            "2 COUNTY OF SAN FRANCISCO\n"
            "3 UNIFIED FAMILY COURT\n"
            "4\n"
            "5\n"
            ")\n"
            "6 JOHN DOE, ) Case Number: FPT-25-100001\n"
            ")\n"
            "7 Petitioner ) Hearing Date: March 3, 2026\n"
            ")\n"
            "8 VS. ) Hearing Time: 9:00 AM\n"
            ")\n"
            "9 JANE DOE, ) Department: 403\n"
            ")\n"
            "10 Respondent ) Presiding: JUDGE SMITH\n"
            ")\n"
            "11 )\n"
            "12 REQUEST FOR ORDER FOR CHILD CUSTODY\n"
            "13 TENTATIVE RULING\n"
            "14 The motion is GRANTED.\n"
            "1 SUPERIOR COURT OF CALIFORNIA\n"
            "2 COUNTY OF SAN FRANCISCO\n"
            "3 UNIFIED FAMILY COURT\n"
            "4\n"
            "5\n"
            ")\n"
            "6 ALICE SMITH, ) Case Number: FDI-25-200002\n"
            ")\n"
            "7 Petitioner ) Hearing Date: March 3, 2026\n"
            ")\n"
            "8 VS. ) Hearing Time: 9:00 AM\n"
            ")\n"
            "9 BOB SMITH, ) Department: 403\n"
            ")\n"
            "10 Respondent ) Presiding: JUDGE SMITH\n"
            ")\n"
            "11 )\n"
            "12 REQUEST FOR ORDER FOR VISITATION\n"
            "13 TENTATIVE RULING\n"
            "14 The request is DENIED.\n"
        )
        event = {"ruling_text": text}
        results = split_sf_document(event)
        assert len(results) == 2

        assert results[0].case_number == "FPT-25-100001"
        assert results[0].case_title is not None
        assert "GRANTED" in results[0].ruling_text
        assert results[0].motion_type is not None
        assert "CHILD CUSTODY" in results[0].motion_type

        assert results[1].case_number == "FDI-25-200002"
        assert "DENIED" in results[1].ruling_text

    def test_boilerplate_discarded(self) -> None:
        """Chunks without case numbers (boilerplate) are discarded."""
        text = (
            "1 SUPERIOR COURT OF CALIFORNIA\n"
            "2 COUNTY OF SAN FRANCISCO\n"
            "3 UNIFIED FAMILY COURT\n"
            "4 NOTICE AND INSTRUCTIONS FOR REMOTE APPEARANCES\n"
            "5 Some boilerplate text without a case number.\n"
            "1 SUPERIOR COURT OF CALIFORNIA\n"
            "2 COUNTY OF SAN FRANCISCO\n"
            "3 UNIFIED FAMILY COURT\n"
            "6 JOHN DOE, ) Case Number: FPT-25-100001\n"
            ")\n"
            "7 Petitioner )\n"
            "8 VS. )\n"
            "9 JANE DOE, )\n"
            "10 Respondent )\n"
            "12 REQUEST FOR ORDER\n"
            "13 TENTATIVE RULING\n"
            "14 Ruling text.\n"
            "1 SUPERIOR COURT OF CALIFORNIA\n"
            "2 COUNTY OF SAN FRANCISCO\n"
            "3 UNIFIED FAMILY COURT\n"
            "6 ALICE SMITH, ) Case Number: FDI-25-200002\n"
            ")\n"
            "7 Petitioner )\n"
            "8 VS. )\n"
            "9 BOB SMITH, )\n"
            "10 Respondent )\n"
            "12 REQUEST FOR ORDER\n"
            "13 TENTATIVE RULING\n"
            "14 More ruling text.\n"
        )
        event = {"ruling_text": text}
        results = split_sf_document(event)
        # Boilerplate (no case number) is discarded; only 2 real cases
        assert len(results) == 2
        assert results[0].case_number == "FPT-25-100001"
        assert results[1].case_number == "FDI-25-200002"

    def test_empty_ruling_text(self) -> None:
        """Returns empty list when ruling_text is empty."""
        event = {"ruling_text": ""}
        results = split_sf_document(event)
        assert results == []

    def test_none_ruling_text(self) -> None:
        """Returns empty list when ruling_text is None."""
        event = {"ruling_text": None}
        results = split_sf_document(event)
        assert results == []

    def test_single_case_returns_empty(self) -> None:
        """A single header block means no splitting needed — return empty list."""
        text = (
            "1 SUPERIOR COURT OF CALIFORNIA\n"
            "2 COUNTY OF SAN FRANCISCO\n"
            "3 UNIFIED FAMILY COURT\n"
            "6 JOHN DOE, ) Case Number: FPT-25-100001\n"
            ")\n"
            "7 Petitioner )\n"
            "8 VS. )\n"
            "9 JANE DOE, )\n"
            "10 Respondent )\n"
            "13 TENTATIVE RULING\n"
            "14 The motion is GRANTED.\n"
        )
        event = {"ruling_text": text}
        results = split_sf_document(event)
        assert len(results) < 2

    def test_no_header_returns_empty(self) -> None:
        """Text without any header blocks returns empty list."""
        event = {"ruling_text": "Some random text without court headers."}
        results = split_sf_document(event)
        assert results == []


# ---------------------------------------------------------------------------
# Framework integration tests
# ---------------------------------------------------------------------------


class TestFrameworkIntegration:
    def test_registered_in_splitter_registry(self) -> None:
        """SF splitter is registered for CA/San Francisco."""
        assert ("CA", "San Francisco") in _splitter_registry

    def test_split_document_dispatches_to_sf(self) -> None:
        """split_document() uses the SF splitter for CA/San Francisco events."""
        text = (
            "1 SUPERIOR COURT OF CALIFORNIA\n"
            "2 COUNTY OF SAN FRANCISCO\n"
            "3 UNIFIED FAMILY COURT\n"
            "6 JOHN DOE, ) Case Number: FPT-25-100001\n"
            ")\n"
            "7 Petitioner )\n"
            "8 VS. )\n"
            "9 JANE DOE, )\n"
            "10 Respondent )\n"
            "13 TENTATIVE RULING\n"
            "14 Ruling one.\n"
            "1 SUPERIOR COURT OF CALIFORNIA\n"
            "2 COUNTY OF SAN FRANCISCO\n"
            "3 UNIFIED FAMILY COURT\n"
            "6 ALICE SMITH, ) Case Number: FDI-25-200002\n"
            ")\n"
            "7 Petitioner )\n"
            "8 VS. )\n"
            "9 BOB SMITH, )\n"
            "10 Respondent )\n"
            "13 TENTATIVE RULING\n"
            "14 Ruling two.\n"
        )
        event = {
            "state": "CA",
            "county": "San Francisco",
            "ruling_text": text,
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) == 2
        assert results[0].case_number == "FPT-25-100001"
        assert results[1].case_number == "FDI-25-200002"


# ---------------------------------------------------------------------------
# Regression tests against real PDF fixture
# ---------------------------------------------------------------------------


class TestSfDept403Fixture:
    """Regression tests using the real SF Dept 403 PDF fixture (sf_dept403_ruling.pdf).

    The fixture contains 11 cases across 30 pages for a single department on a
    single hearing date (March 3, 2026).  Pages 1-2 are boilerplate instructions.
    """

    @pytest.fixture()
    def sf_text(self) -> str:
        return _extract_pdf_text(_load_bytes("sf_dept403_ruling.pdf"))

    def test_splits_into_11_cases(self, sf_text: str) -> None:
        """The fixture should split into exactly 11 individual rulings."""
        results = split_sf_document({"ruling_text": sf_text})
        assert len(results) == 11

    def test_each_result_has_case_number(self, sf_text: str) -> None:
        """Every split result must have a valid SF case number."""
        results = split_sf_document({"ruling_text": sf_text})
        for r in results:
            assert r.case_number is not None, (
                f"Missing case_number in chunk starting: {r.ruling_text[:100]}"
            )
            assert _is_sf_case_number(r.case_number), f"Invalid case number format: {r.case_number}"

    def test_expected_case_numbers(self, sf_text: str) -> None:
        """Verify the expected case numbers are extracted in order."""
        results = split_sf_document({"ruling_text": sf_text})
        expected = [
            "FPT-25-378624",
            "FPT-25-378672",
            "FMS-20-387302",
            "FDI-14-781786",
            "FDI-16-786194",
            "FDI-17-787957",
            "FDI-20-794120",
            "FDI-25-800840",
            "FDI-25-801374",
            "FDI-25-802212",
            "FDV-21-815942",
        ]
        actual = [r.case_number for r in results]
        assert actual == expected

    def test_case_titles_extracted(self, sf_text: str) -> None:
        """Each split result should have a case title with petitioner v. respondent."""
        results = split_sf_document({"ruling_text": sf_text})
        for r in results:
            assert r.case_title is not None, f"Missing case_title for {r.case_number}"
            assert "v." in r.case_title, (
                f"Case title missing 'v.' separator for {r.case_number}: {r.case_title}"
            )

    def test_first_case_graves_v_long(self, sf_text: str) -> None:
        """First case: Graves v. Long — case FPT-25-378624."""
        results = split_sf_document({"ruling_text": sf_text})
        first = results[0]
        assert first.case_number == "FPT-25-378624"
        assert first.case_title is not None
        assert "GRAVES" in first.case_title.upper()
        assert "LONG" in first.case_title.upper()

    def test_ruling_text_isolation(self, sf_text: str) -> None:
        """Each ruling's text should contain only that case's content."""
        results = split_sf_document({"ruling_text": sf_text})
        # First case (Graves v. Long) should not contain text from second case (Ramirez Hernandez)
        first_upper = results[0].ruling_text.upper()
        assert "RAMIREZ" not in first_upper or "FPT-25-378672" not in results[0].ruling_text
        # Last case should not contain first case content
        assert results[0].case_number not in results[-1].ruling_text

    def test_boilerplate_not_returned(self, sf_text: str) -> None:
        """Boilerplate instruction pages (no case number) are not returned."""
        results = split_sf_document({"ruling_text": sf_text})
        for r in results:
            # No result should be boilerplate (would lack case number)
            assert r.case_number is not None
            # Boilerplate contains "Important Information for Tentative Rulings"
            assert "Important Information for Tentative Rulings" not in r.ruling_text

    def test_each_result_has_unique_ruling_text(self, sf_text: str) -> None:
        """Each split result should have distinct ruling text."""
        results = split_sf_document({"ruling_text": sf_text})
        texts = [r.ruling_text for r in results]
        assert len(set(texts)) == len(texts)

    def test_motion_types_extracted(self, sf_text: str) -> None:
        """Most results should have a motion type extracted."""
        results = split_sf_document({"ruling_text": sf_text})
        with_motion = [r for r in results if r.motion_type is not None]
        # All 11 cases have REQUEST FOR ORDER headings
        assert len(with_motion) >= 10, f"Only {len(with_motion)}/11 results have motion_type"

    def test_first_case_motion_type(self, sf_text: str) -> None:
        """First case motion type should mention child custody."""
        results = split_sf_document({"ruling_text": sf_text})
        assert results[0].motion_type is not None
        assert "CHILD CUSTODY" in results[0].motion_type.upper()

    def test_split_via_framework(self, sf_text: str) -> None:
        """Verify the full framework pipeline works for SF documents."""
        event = {
            "state": "CA",
            "county": "San Francisco",
            "ruling_text": sf_text,
            "case_title": None,
            "case_number": None,
            "motion_type": None,
            "outcome": None,
        }
        results = split_document(event)
        assert len(results) == 11
        assert results[0].case_number == "FPT-25-378624"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_sf_case_number(case_number: str) -> bool:
    """Validate that a case number matches the SF format."""
    import re

    return bool(re.match(r"^F[A-Z]{2}-\d{2}-\d{6}$", case_number))
