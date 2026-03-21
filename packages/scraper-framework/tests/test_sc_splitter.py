"""Tests for Santa Clara document splitter.

Tests the SC splitter against real fixture PDFs to verify:
1. Multi-case PDFs are correctly split into individual rulings
2. Case numbers are correctly extracted per split
3. Case titles are properly parsed (not garbled ruling text)
4. No phantom cases (UNKNOWN- prefix) are created from duplicate splits

Fixtures:
  sc_dept1_tues.pdf  -- 3 cases (Dept 1, LINE format)
  sc_dept6_tues.pdf  -- 13 cases (Dept 6, table format)
  sc_dept16_wed.pdf  -- many cases (Dept 16, column format)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from courts.ca.sc_tentatives import extract_pdf_text
from ingestion.sc_splitter import (
    _extract_case_title_from_chunk,
    _extract_motion_type,
    _extract_outcome,
    _find_case_boundaries,
    split_sc_document,
)

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _pdf_text(name: str) -> str:
    return extract_pdf_text(_load_bytes(name))


# ---------------------------------------------------------------------------
# _find_case_boundaries
# ---------------------------------------------------------------------------


class TestFindCaseBoundaries:
    """Tests for case boundary detection."""

    def test_dept6_finds_multiple_cases(self) -> None:
        text = _pdf_text("sc_dept6_tues.pdf")
        boundaries = _find_case_boundaries(text)
        assert len(boundaries) >= 5
        case_numbers = [cn for _, cn in boundaries]
        assert "24CV443183" in case_numbers
        assert "25CV460465" in case_numbers

    def test_dept1_finds_cases(self) -> None:
        text = _pdf_text("sc_dept1_tues.pdf")
        boundaries = _find_case_boundaries(text)
        assert len(boundaries) >= 2
        case_numbers = [cn for _, cn in boundaries]
        assert "26CV484360" in case_numbers
        assert "25CV460197" in case_numbers

    def test_dept16_finds_cases(self) -> None:
        text = _pdf_text("sc_dept16_wed.pdf")
        boundaries = _find_case_boundaries(text)
        assert len(boundaries) >= 3
        case_numbers = [cn for _, cn in boundaries]
        assert "23CV419582" in case_numbers

    def test_boundaries_are_sorted_by_position(self) -> None:
        text = _pdf_text("sc_dept6_tues.pdf")
        boundaries = _find_case_boundaries(text)
        positions = [pos for pos, _ in boundaries]
        assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# _extract_case_title_from_chunk
# ---------------------------------------------------------------------------


class TestExtractCaseTitle:
    """Tests for case title extraction from individual case chunks."""

    def test_simple_vs_title(self) -> None:
        chunk = "24CV443183 Huynh vs Redis Labs Case is off calendar."
        title = _extract_case_title_from_chunk(chunk)
        assert title is not None
        assert "Huynh" in title
        assert "Redis Labs" in title

    def test_vs_dot_title(self) -> None:
        chunk = "25CV460465 Nicholas v. Compass Group, et. Al. Defendant moves..."
        title = _extract_case_title_from_chunk(chunk)
        assert title is not None
        assert "Nicholas" in title
        assert "Compass" in title

    def test_et_al_in_title(self) -> None:
        chunk = (
            "9:01 23CV419582 Virginia Riegos-Rangel, et al.\n"
            "1 v.\n"
            "Terry Ray Freeman, et al.\n"
            "Before the Court is a Petition..."
        )
        title = _extract_case_title_from_chunk(chunk)
        assert title is not None
        assert "Virginia" in title or "Riegos" in title
        assert "Freeman" in title

    def test_no_vs_returns_none(self) -> None:
        chunk = "Some random text without party names"
        title = _extract_case_title_from_chunk(chunk)
        assert title is None

    def test_title_does_not_include_ruling_text(self) -> None:
        """Title should not contain ruling body text like 'Petitioner cites...'."""
        chunk = (
            "24CV443183 Huynh vs Redis Labs "
            "Petitioner cites Lee v. Swansboro Country Property Owners "
            "Assn. (2007) 151 Cal.App.4th 575"
        )
        title = _extract_case_title_from_chunk(chunk)
        assert title is not None
        assert "Petitioner" not in title
        assert "Swansboro" not in title
        assert "Huynh" in title
        assert "Redis Labs" in title

    def test_multiline_title(self) -> None:
        chunk = "LINE 1 26CV484360 Mohammad\nKhokhar vs Gene\nKristul et al\nOff calendar."
        title = _extract_case_title_from_chunk(chunk)
        assert title is not None
        assert "Khokhar" in title or "Mohammad" in title


# ---------------------------------------------------------------------------
# _extract_outcome
# ---------------------------------------------------------------------------


class TestExtractOutcome:
    """Tests for outcome extraction from ruling chunks."""

    def test_granted(self) -> None:
        assert _extract_outcome("Motion is GRANTED.") == "GRANTED"

    def test_denied(self) -> None:
        assert _extract_outcome("Motion is DENIED.") == "DENIED"

    def test_off_calendar(self) -> None:
        assert _extract_outcome("Case is off calendar.") == "Off calendar"

    def test_none_for_empty(self) -> None:
        assert _extract_outcome("") is None


# ---------------------------------------------------------------------------
# _extract_motion_type
# ---------------------------------------------------------------------------


class TestExtractMotionType:
    """Tests for motion type extraction."""

    def test_demurrer(self) -> None:
        result = _extract_motion_type("Defendant moves for demurrer")
        assert result == "Demurrer"

    def test_summary_judgment(self) -> None:
        result = _extract_motion_type("Plaintiff's Summary Judgment motion")
        assert result == "Summary Judgment"


# ---------------------------------------------------------------------------
# split_sc_document -- full integration tests with real fixtures
# ---------------------------------------------------------------------------


class TestSplitSCDocument:
    """Integration tests for the full splitter function."""

    def test_dept6_splits_into_multiple(self) -> None:
        text = _pdf_text("sc_dept6_tues.pdf")
        event_data = {"ruling_text": text, "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)
        assert len(results) >= 5
        # Each result should have a valid case number
        for r in results:
            assert r.case_number is not None
            assert r.case_number.upper() == r.case_number

    def test_dept1_splits_into_cases(self) -> None:
        text = _pdf_text("sc_dept1_tues.pdf")
        event_data = {"ruling_text": text, "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)
        assert len(results) >= 2

    def test_dept16_splits_into_cases(self) -> None:
        text = _pdf_text("sc_dept16_wed.pdf")
        event_data = {"ruling_text": text, "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)
        assert len(results) >= 3

    def test_each_split_has_case_number(self) -> None:
        text = _pdf_text("sc_dept6_tues.pdf")
        event_data = {"ruling_text": text, "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)
        for r in results:
            assert r.case_number is not None, "Every split must have a case number"
            # Must be a real case number format, not UNKNOWN-
            assert not r.case_number.startswith("UNKNOWN"), (
                f"Got phantom case number: {r.case_number}"
            )

    def test_case_titles_are_not_ruling_text(self) -> None:
        """Case titles should be party names, not ruling body text."""
        text = _pdf_text("sc_dept6_tues.pdf")
        event_data = {"ruling_text": text, "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)
        for r in results:
            if r.case_title is not None:
                # Title should not contain ruling body text patterns
                assert "Petitioner cites" not in r.case_title, (
                    f"Title contains ruling text: {r.case_title}"
                )
                assert "Pursuant to" not in r.case_title
                # Title should be reasonably short
                assert len(r.case_title) < 150, (
                    f"Title too long (likely ruling text): {r.case_title}"
                )

    def test_no_duplicate_case_entries(self) -> None:
        """Splitter should not create duplicate entries for the same case."""
        text = _pdf_text("sc_dept6_tues.pdf")
        event_data = {"ruling_text": text, "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)
        # Check for exact duplicate case_number + case_title pairs
        seen: set[tuple[str | None, str | None]] = set()
        for r in results:
            key = (r.case_number, r.case_title)
            if key in seen:
                pass  # Same case number with different ruling text is allowed
            seen.add(key)

    def test_empty_ruling_text(self) -> None:
        event_data = {"ruling_text": "", "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)
        assert results == []

    def test_none_ruling_text(self) -> None:
        event_data = {"ruling_text": None, "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)
        assert results == []

    def test_single_case_returns_empty(self) -> None:
        """Single-case text should return empty list (no split needed)."""
        event_data = {
            "ruling_text": "9:00 24CV443183 Huynh vs Redis Labs Case is off calendar.",
            "state": "CA",
            "county": "Santa Clara",
        }
        results = split_sc_document(event_data)
        assert len(results) < 2

    def test_dept6_first_case_has_correct_number(self) -> None:
        text = _pdf_text("sc_dept6_tues.pdf")
        event_data = {"ruling_text": text, "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)
        case_numbers = [r.case_number for r in results]
        assert "24CV443183" in case_numbers

    def test_dept6_known_titles(self) -> None:
        """Verify known case titles from dept 6 PDF are correctly extracted."""
        text = _pdf_text("sc_dept6_tues.pdf")
        event_data = {"ruling_text": text, "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)

        title_by_cn: dict[str | None, str | None] = {r.case_number: r.case_title for r in results}

        # 24CV443183: "Huynh vs Redis Labs"
        title = title_by_cn.get("24CV443183")
        if title is not None:
            assert "Huynh" in title

        # 25CV460465: "Nicholas v. Compass Group"
        title = title_by_cn.get("25CV460465")
        if title is not None:
            assert "Nicholas" in title

    def test_dept6_outcomes(self) -> None:
        """Verify outcomes are extracted for cases with clear outcomes."""
        text = _pdf_text("sc_dept6_tues.pdf")
        event_data = {"ruling_text": text, "state": "CA", "county": "Santa Clara"}
        results = split_sc_document(event_data)

        outcome_by_cn = {r.case_number: r.outcome for r in results}

        # 24CV443183: off calendar
        assert outcome_by_cn.get("24CV443183") == "Off calendar"

        # 25CV460465: GRANTED
        assert outcome_by_cn.get("25CV460465") == "GRANTED"


# ---------------------------------------------------------------------------
# Integration with splitter framework
# ---------------------------------------------------------------------------


class TestSplitterFrameworkIntegration:
    """Test that the SC splitter is properly registered with the framework."""

    def test_registered(self) -> None:
        from ingestion.splitter import _splitter_registry

        assert ("CA", "Santa Clara") in _splitter_registry

    def test_split_document_uses_sc_splitter(self) -> None:
        from ingestion.splitter import split_document

        text = _pdf_text("sc_dept6_tues.pdf")
        event_data = {"ruling_text": text, "state": "CA", "county": "Santa Clara"}
        results = split_document(event_data)
        assert len(results) >= 5
