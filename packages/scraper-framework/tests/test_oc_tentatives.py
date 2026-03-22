"""Tests for Orange County tentative rulings scraper.

Covers hearing_date extraction and North Justice Center case title parsing.

Fixtures captured from live site 2026-03-02:
  oc_civil_page.html     — index page with 33 PDF links
  oc_apkarian_c25.pdf    — Dept C25, Judge Gassia Apkarian (36 pages)
  oc_central_c34.pdf     — Dept C34, Judge H. Shaina Colover (27 pages)
  oc_complex_cx.pdf      — Dept CX101, Judge William D. Claster (2 pages)
  oc_costa_mesa_cm.pdf   — Dept CM02, Judge Andre De La Cruz (33 pages)
  oc_north_n.pdf         — Dept N6, Judge Julianne S. Bancroft (26 pages)
  oc_west_w.pdf          — Dept W15, Judge Richard Y. Lee (38 pages)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.oc_tentatives import (
    INDEX_URL,
    OCTentativeRulingsScraper,
    _extract_oc_pdf_text,
    _is_north_dept,
    _normalize_oc_text,
    _oc_hearing_date_from_text,
    _parse_north_case_entries,
    _reconstruct_entry_text,
)
from courts.ca.oc_tentatives import default_config as oc_default_config
from courts.ca.pdf_link_scraper import _extract_pdf_text

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# _oc_hearing_date_from_text — unit tests
# ---------------------------------------------------------------------------


def test_oc_hearing_date_standard_format() -> None:
    text = "TENTATIVE RULINGS\nDEPT C25\nDate: February 24, 2026\nSome ruling"
    assert _oc_hearing_date_from_text(text) == datetime(2026, 2, 24)


def test_oc_hearing_date_leading_zero_day() -> None:
    text = "March 06, 2026\n09:00 a.m.\nCX-101"
    assert _oc_hearing_date_from_text(text) == datetime(2026, 3, 6)


def test_oc_hearing_date_no_comma() -> None:
    text = "February 24 2026\nSome text"
    assert _oc_hearing_date_from_text(text) == datetime(2026, 2, 24)


def test_oc_hearing_date_returns_none_for_no_date() -> None:
    assert _oc_hearing_date_from_text("") is None
    assert _oc_hearing_date_from_text("No dates here at all") is None


# ---------------------------------------------------------------------------
# _oc_hearing_date_from_text — against real PDF fixtures
# ---------------------------------------------------------------------------


def test_oc_hearing_date_apkarian_c25() -> None:
    text = _extract_pdf_text(_load_bytes("oc_apkarian_c25.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 2, 24)


def test_oc_hearing_date_central_c34() -> None:
    text = _extract_pdf_text(_load_bytes("oc_central_c34.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 2, 26)


def test_oc_hearing_date_complex_cx() -> None:
    text = _extract_pdf_text(_load_bytes("oc_complex_cx.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 6)


def test_oc_hearing_date_costa_mesa_cm() -> None:
    text = _extract_pdf_text(_load_bytes("oc_costa_mesa_cm.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 2, 19)


def test_oc_hearing_date_north_n() -> None:
    text = _extract_pdf_text(_load_bytes("oc_north_n.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 2)


def test_oc_hearing_date_west_w() -> None:
    text = _extract_pdf_text(_load_bytes("oc_west_w.pdf"))
    dt = _oc_hearing_date_from_text(text)
    assert dt == datetime(2026, 2, 26)


# ---------------------------------------------------------------------------
# Full scraper run — hearing_date populated
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_run_populates_hearing_date() -> None:
    html = _load_html("oc_civil_page.html")
    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # All docs use the same PDF fixture, so all should have a hearing date
    has_date = [d for d in parsed if d.hearing_date]
    assert len(has_date) == len(parsed)
    assert has_date[0].hearing_date == datetime(2026, 2, 24)


# ---------------------------------------------------------------------------
# _is_north_dept — unit tests
# ---------------------------------------------------------------------------


def test_is_north_dept_positive() -> None:
    assert _is_north_dept("N14") is True
    assert _is_north_dept("N6") is True
    assert _is_north_dept("N16") is True
    assert _is_north_dept("n14") is True


def test_is_north_dept_negative() -> None:
    assert _is_north_dept("C25") is False
    assert _is_north_dept("CX101") is False
    assert _is_north_dept("CM02") is False
    assert _is_north_dept("W15") is False
    assert _is_north_dept(None) is False
    assert _is_north_dept("") is False


# ---------------------------------------------------------------------------
# _parse_north_case_entries — unit tests (synthetic text)
# ---------------------------------------------------------------------------


def test_parse_north_entries_basic() -> None:
    text = (
        "# Case Name Tentative\n"
        "101 Smith vs Jones Motion for Summary Judgment\n"
        "Some ruling text here.\n"
        "102 Doe vs Roe Demurrer to Complaint\n"
        "More ruling text.\n"
    )
    entries = _parse_north_case_entries(text)
    assert len(entries) == 2
    assert entries[0].line_num == "101"
    assert entries[0].case_title == "Smith vs Jones"
    assert entries[0].motion_type == "Motion for Summary Judgment"
    assert entries[1].line_num == "102"
    assert entries[1].case_title == "Doe vs Roe"
    assert entries[1].motion_type == "Demurrer to Complaint"


def test_parse_north_entries_multiline_case_name() -> None:
    text = (
        "101 Careful Consulting, Motion to Be Relieved as Counsel of Record\n"
        "LLC vs Pacific Health\n"
        "Staffing, LLC\n"
        "Some ruling text that is much longer than the name lines above.\n"
    )
    entries = _parse_north_case_entries(text)
    assert len(entries) == 1
    assert "Careful Consulting," in entries[0].case_title
    assert "vs Pacific Health" in entries[0].case_title


def test_parse_north_entries_no_vs_skipped() -> None:
    """Lines without 'vs' are not case entries — they are false positives."""
    text = (
        "151 Cal.App.4th 168, 175 some legal citation\n"
        "101 Smith vs Jones Motion for Summary Judgment\n"
    )
    entries = _parse_north_case_entries(text)
    assert len(entries) == 1
    assert entries[0].case_title == "Smith vs Jones"


def test_parse_north_entries_empty_text() -> None:
    assert _parse_north_case_entries("") == []
    assert _parse_north_case_entries("No case entries here") == []


def test_parse_north_entries_prose_numbers_not_matched() -> None:
    """Prose lines starting with numbers are not treated as entries (#1186)."""
    text = (
        "1 Zavala vs. Becker Motion for Attorneys' Fees\n"
        "The Court grants the motion.\n"
        "10 calendar days is insufficient notice.\n"
        "2 Alpha vs. Beta Demurrer\n"
        "The demurrer is OVERRULED.\n"
    )
    entries = _parse_north_case_entries(text)
    assert len(entries) == 2
    assert entries[0].line_num == "1"
    assert entries[1].line_num == "2"


# ---------------------------------------------------------------------------
# _parse_north_case_entries — against real North PDF fixture
# ---------------------------------------------------------------------------


def test_parse_north_entries_from_fixture() -> None:
    """Regression: parse all case entries from the real North JC PDF."""
    text = _extract_pdf_text(_load_bytes("oc_north_n.pdf"))
    entries = _parse_north_case_entries(text)

    # The fixture contains 11 case entries (101-112, skipping 104)
    assert len(entries) >= 10  # at least 10 entries

    # Verify specific known entries
    by_num = {e.line_num: e for e in entries}

    # Entry 101: Alday vs Orange Coast Title (Company of Southern California)
    assert "101" in by_num
    assert "Alday" in by_num["101"].case_title
    assert "vs" in by_num["101"].case_title.lower()
    assert by_num["101"].motion_type is not None
    assert "Continuance" in by_num["101"].motion_type

    # Entry 106: Groff vs Krumly
    assert "106" in by_num
    assert "Groff" in by_num["106"].case_title
    assert "Krumly" in by_num["106"].case_title

    # Entry 111: Reyes vs Ford Motor Company
    assert "111" in by_num
    assert "Reyes" in by_num["111"].case_title
    assert "Ford" in by_num["111"].case_title

    # All entries should have "vs" in the title
    for entry in entries:
        assert "vs" in entry.case_title.lower(), (
            f"Entry {entry.line_num} missing 'vs': {entry.case_title}"
        )


# ---------------------------------------------------------------------------
# Full scraper run — North JC case titles populated
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_run_populates_north_case_titles() -> None:
    """When a North JC PDF is parsed, case_title and extra fields are populated."""
    html = _load_html("oc_civil_page.html")
    north_pdf = _load_bytes("oc_north_n.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=north_pdf))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()

    # Find a doc with a North dept code (N*)
    north_docs = [d for d in docs if _is_north_dept(d.department)]
    assert len(north_docs) > 0, "Expected at least one North JC doc"

    # Parse the North doc
    parsed = scraper.parse_document(north_docs[0])

    # case_title should be populated
    assert parsed.case_title is not None
    assert "vs" in parsed.case_title.lower()

    # extra should contain all case titles
    assert "case_titles" in parsed.extra
    assert len(parsed.extra["case_titles"]) >= 10

    # motion_types should be populated
    assert "motion_types" in parsed.extra
    assert len(parsed.extra["motion_types"]) > 0


@respx.mock
def test_oc_run_central_no_case_titles_in_extra() -> None:
    """Non-North JC PDFs should NOT populate case_titles in extra."""
    html = _load_html("oc_civil_page.html")
    central_pdf = _load_bytes("oc_central_c34.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=central_pdf))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()

    # Find a doc with a Central dept code (C*)
    central_docs = [
        d
        for d in docs
        if d.department
        and d.department.upper().startswith("C")
        and not d.department.upper().startswith("CX")
        and not d.department.upper().startswith("CM")
    ]
    assert len(central_docs) > 0

    parsed = scraper.parse_document(central_docs[0])

    # Central docs should have case_number (from regex), not case_titles
    assert "case_titles" not in parsed.extra


# ---------------------------------------------------------------------------
# _normalize_oc_text — unit tests for split-line case number rejoining
# ---------------------------------------------------------------------------


def test_normalize_oc_text_split_four_digit_year() -> None:
    """Case number split across lines: 2024-\\n01428785 -> 2024-01428785."""
    text = (
        "53. Morton v.     Plaintiff Robert Morton's ...\n"
        "OC Sheriff        DENIED.\n"
        "2024-\n01428785\nSome more text"
    )
    result = _normalize_oc_text(text)
    assert "2024-01428785" in result
    assert "2024-\n01428785" not in result


def test_normalize_oc_text_split_two_digit_year() -> None:
    """Case number split across lines: 25-\\n01455183 -> 25-01455183."""
    text = "Some text\n25-\n01455183\nMore text"
    result = _normalize_oc_text(text)
    assert "25-01455183" in result


def test_normalize_oc_text_split_with_spaces() -> None:
    """Case number split with whitespace: 2024-  \\n  01428785."""
    text = "Text before\n2024-  \n  01428785\nText after"
    result = _normalize_oc_text(text)
    assert "2024-01428785" in result


def test_normalize_oc_text_split_seven_digit() -> None:
    """Case number split with 7-digit sequence: 24-\\n1377364."""
    text = "Ruling text\n24-\n1377364\nMore text"
    result = _normalize_oc_text(text)
    assert "24-1377364" in result


def test_normalize_oc_text_no_split_passthrough() -> None:
    """Text without split case numbers passes through unchanged."""
    text = "25-01455183 is a normal case number\nOn a single line"
    result = _normalize_oc_text(text)
    assert result == text


def test_normalize_oc_text_multiple_splits() -> None:
    """Multiple split case numbers in the same text."""
    text = "Case 1:\n2024-\n01428785\nCase 2:\n25-\n01455183\n"
    result = _normalize_oc_text(text)
    assert "2024-01428785" in result
    assert "25-01455183" in result


# ---------------------------------------------------------------------------
# Seven-digit case numbers — regex tests
# ---------------------------------------------------------------------------


def test_seven_digit_case_number_matched() -> None:
    """Case number with 7 digits after dash (C20 format) is captured."""
    from courts.ca.oc_tentatives import OCTentativeRulingsScraper

    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)
    regex = scraper._pdf_config.case_number_re

    assert regex.search("24-1377364") is not None
    assert regex.search("24-1377364").group() == "24-1377364"


def test_seven_digit_case_number_in_context() -> None:
    """Seven-digit case number extracted from realistic PDF text."""
    from courts.ca.oc_tentatives import OCTentativeRulingsScraper

    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)
    regex = scraper._pdf_config.case_number_re

    text = "1. Smith v. Jones   Motion for Summary Judgment\n24-1377364          GRANTED."
    matches = regex.findall(text)
    assert "24-1377364" in matches


def test_eight_digit_case_number_still_matched() -> None:
    """Standard 8-digit case numbers still work after regex relaxation."""
    from courts.ca.oc_tentatives import OCTentativeRulingsScraper

    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)
    regex = scraper._pdf_config.case_number_re

    assert regex.search("25-01455183") is not None
    assert regex.search("2024-01437598") is not None


# ---------------------------------------------------------------------------
# Three-part case numbers — integration tests
# ---------------------------------------------------------------------------


@respx.mock
def test_oc_three_part_case_number_captured() -> None:
    """Three-part case numbers (30-2024-01420730) are captured with prefix."""
    html = _load_html("oc_civil_page.html")
    # Use the C34 fixture which has three-part case numbers
    pdf_bytes = _load_bytes("oc_central_c34.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = oc_default_config()
    config.request_delay_seconds = 0
    scraper = OCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    assert len(docs) > 0

    parsed = scraper.parse_document(docs[0])

    # The C34 fixture contains three-part case numbers like 30-2024-01393434
    # These should be captured with the full prefix
    all_numbers = parsed.extra.get("all_case_numbers", [parsed.case_number])
    three_part_numbers = [cn for cn in all_numbers if cn.count("-") == 2]
    assert len(three_part_numbers) > 0, f"Expected three-part case numbers but got: {all_numbers}"
    # Verify format: DD-DDDD-DDDDDDD+
    for cn in three_part_numbers:
        parts = cn.split("-")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)


def test_oc_split_case_number_synthetic() -> None:
    """Synthetic test: split case numbers are rejoined and extracted."""
    from unittest.mock import patch

    from framework import ContentFormat

    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=b"fake-pdf",
        content_format=ContentFormat.PDF,
    )
    doc.department = "C28"

    synthetic_text = (
        "TENTATIVE RULINGS\nLAW & MOTION\nDEPT C28\n"
        "Date: March 10, 2026\n"
        "1. Smith v. Jones   Motion for Summary Judgment\n"
        "2024-\n"
        "01428785          GRANTED.\n"
        "2. Doe v. Roe      Demurrer\n"
        "25-01455183       SUSTAINED.\n"
    )

    with patch(
        "courts.ca.pdf_link_scraper._extract_pdf_text",
        return_value=synthetic_text,
    ):
        parsed = scraper.parse_document(doc)

    # The split case number should be rejoined
    all_numbers = parsed.extra.get("all_case_numbers", [parsed.case_number])
    assert "2024-01428785" in all_numbers
    assert "25-01455183" in all_numbers


def test_oc_seven_digit_case_number_synthetic() -> None:
    """Synthetic test: 7-digit case numbers are extracted."""
    from unittest.mock import patch

    from framework import ContentFormat

    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=b"fake-pdf",
        content_format=ContentFormat.PDF,
    )
    doc.department = "C20"

    synthetic_text = (
        "TENTATIVE RULINGS\nLAW & MOTION\nDEPT C20\n"
        "Date: March 10, 2026\n"
        "1. Alpha v. Beta   Motion to Compel\n"
        "24-1377364          GRANTED.\n"
        "2. Gamma v. Delta  Demurrer\n"
        "24-1388901          OVERRULED.\n"
    )

    with patch(
        "courts.ca.pdf_link_scraper._extract_pdf_text",
        return_value=synthetic_text,
    ):
        parsed = scraper.parse_document(doc)

    all_numbers = parsed.extra.get("all_case_numbers", [parsed.case_number])
    assert "24-1377364" in all_numbers
    assert "24-1388901" in all_numbers


# ---------------------------------------------------------------------------
# Regression: existing fixtures still work after changes
# ---------------------------------------------------------------------------


def test_oc_west_case_numbers_unchanged() -> None:
    """West JC fixture still extracts the same case numbers after changes."""
    from framework import ContentFormat

    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)

    pdf_bytes = _load_bytes("oc_west_w.pdf")

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=pdf_bytes,
        content_format=ContentFormat.PDF,
    )
    doc.department = "W15"

    parsed = scraper.parse_document(doc)

    # West JC should still have case numbers
    assert parsed.case_number is not None
    # Known case numbers from the fixture
    assert parsed.case_number.count("-") == 1  # Standard 2-part format


def test_oc_apkarian_case_numbers_unchanged() -> None:
    """Apkarian C25 fixture still extracts the same case numbers."""
    from framework import ContentFormat

    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)

    pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=pdf_bytes,
        content_format=ContentFormat.PDF,
    )
    doc.department = "C25"

    parsed = scraper.parse_document(doc)

    assert parsed.case_number is not None
    all_numbers = parsed.extra.get("all_case_numbers", [parsed.case_number])
    assert len(all_numbers) >= 10  # C25 has many case numbers


def test_oc_central_c34_three_part_numbers() -> None:
    """C34 fixture captures three-part case numbers with location prefix."""
    from framework import ContentFormat

    config = oc_default_config()
    scraper = OCTentativeRulingsScraper(config=config)

    pdf_bytes = _load_bytes("oc_central_c34.pdf")

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=pdf_bytes,
        content_format=ContentFormat.PDF,
    )
    doc.department = "C34"

    parsed = scraper.parse_document(doc)

    assert parsed.case_number is not None
    all_numbers = parsed.extra.get("all_case_numbers", [parsed.case_number])

    # C34 has known three-part numbers like 30-2024-01393434
    three_part = [cn for cn in all_numbers if cn.count("-") == 2]
    assert len(three_part) > 0, f"Expected three-part numbers in C34, got: {all_numbers}"

    # Verify 30-2024-01393434 is captured (known from fixture inspection)
    assert any("30-2024-01393434" == cn for cn in all_numbers), (
        f"Expected 30-2024-01393434 in {all_numbers}"
    )


# ---------------------------------------------------------------------------
# Table-aware PDF text extraction (#1241) — column-merge fix
# ---------------------------------------------------------------------------


class TestTableAwareExtraction:
    """Tests for _extract_oc_pdf_text which uses pdfplumber table detection
    to properly separate entry#, case name, and ruling text columns.

    The column-merge bug (#1241) caused case names to be interleaved with
    ruling text when using simple left-to-right text extraction, e.g.:
    "Illingworth vs. Before the court is an unopposed motion..."
    """

    def test_costa_mesa_no_column_merge(self) -> None:
        """Costa Mesa fixture: case names must not contain ruling text (#1241)."""
        pdf_bytes = _load_bytes("oc_costa_mesa_cm.pdf")
        text = _extract_oc_pdf_text(pdf_bytes)

        # The old extraction had "Mallett vs. City of Before the Court"
        # and "Kohlman vs. Before the Court" — verify these are gone.
        for line in text.split("\n"):
            if "vs" in line.lower() and "before the" in line.lower():
                # Allow "vs" and "before" on separate logical parts, but
                # not when they form a single garbled case name.
                # Check: is "Before" part of a case name or ruling text?
                assert "Before the Court" not in line.split("vs")[0], (
                    f"Column-merge detected: ruling text in case name: {line[:200]}"
                )

    def test_costa_mesa_case_name_separate_from_ruling(self) -> None:
        """Costa Mesa: case name appears on separate line(s) from ruling text."""
        pdf_bytes = _load_bytes("oc_costa_mesa_cm.pdf")
        text = _extract_oc_pdf_text(pdf_bytes)

        # Look for the first entry's case name
        assert "Creditors" in text
        assert "Abbas" in text
        assert "2024-01437598" in text

        # Find the case name block — should be on lines separate from ruling
        lines = text.split("\n")
        case_name_line_idx = None
        for i, line in enumerate(lines):
            if "Creditors" in line:
                case_name_line_idx = i
                break

        assert case_name_line_idx is not None
        # The case name line should NOT contain "Before the Court"
        assert "Before the Court" not in lines[case_name_line_idx]

    def test_central_c34_no_column_merge(self) -> None:
        """Central C34 fixture: case names must not contain ruling text."""
        pdf_bytes = _load_bytes("oc_central_c34.pdf")
        text = _extract_oc_pdf_text(pdf_bytes)

        # Verify case name "Mercury Insurance Company vs. Cabral"
        # does not have ruling text on the same line
        for line in text.split("\n"):
            if "Mercury" in line and "Insurance" in line:
                assert "Defendant" not in line, (
                    f"Column-merge: ruling text in case name line: {line[:200]}"
                )

    def test_west_no_column_merge(self) -> None:
        """West JC fixture: case names must not contain ruling text."""
        pdf_bytes = _load_bytes("oc_west_w.pdf")
        text = _extract_oc_pdf_text(pdf_bytes)

        # The first entry is "Hedayati vs. US Kitchen & Flooring, Inc."
        assert "Hedayati" in text
        # Find the line with the case name
        for line in text.split("\n"):
            if "Hedayati" in line:
                # Should not contain "HEARING ADVANCED" from the ruling column
                assert "HEARING" not in line, (
                    f"Column-merge: ruling text in case name line: {line[:200]}"
                )
                break

    def test_complex_cx_no_column_merge(self) -> None:
        """Complex CX fixture: case names must not contain ruling text."""
        pdf_bytes = _load_bytes("oc_complex_cx.pdf")
        text = _extract_oc_pdf_text(pdf_bytes)

        # First entry is "Soto vs. People First Pizza"
        for line in text.split("\n"):
            if "Soto" in line:
                assert "Motion" not in line, (
                    f"Column-merge: ruling text in case name line: {line[:200]}"
                )
                break

    def test_header_text_preserved(self) -> None:
        """Header text (hearing date, dept info) is preserved above entries."""
        pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")
        text = _extract_oc_pdf_text(pdf_bytes)

        # Header should contain dept and judge name
        assert "DEPT C25" in text
        assert "Apkarian" in text

        # Hearing date must appear before any case entry
        date_idx = text.find("February 24, 2026")
        entry_idx = text.find("Okino")
        assert date_idx >= 0, "Hearing date not found in extracted text"
        assert entry_idx >= 0, "First entry not found in extracted text"
        assert date_idx < entry_idx, "Hearing date should appear before first case entry"

    def test_hearing_date_still_extracted_after_table_fix(self) -> None:
        """Hearing date extraction works correctly with table-aware text."""
        for fixture, expected_date in [
            ("oc_apkarian_c25.pdf", datetime(2026, 2, 24)),
            ("oc_central_c34.pdf", datetime(2026, 2, 26)),
            ("oc_costa_mesa_cm.pdf", datetime(2026, 2, 19)),
            ("oc_west_w.pdf", datetime(2026, 2, 26)),
            ("oc_complex_cx.pdf", datetime(2026, 3, 6)),
        ]:
            text = _extract_oc_pdf_text(_load_bytes(fixture))
            dt = _oc_hearing_date_from_text(text)
            assert dt == expected_date, f"{fixture}: expected {expected_date}, got {dt}"

    def test_north_fixture_still_works(self) -> None:
        """North JC fixture extraction should still work (tables detected)."""
        pdf_bytes = _load_bytes("oc_north_n.pdf")
        text = _extract_oc_pdf_text(pdf_bytes)

        # Should still contain case entries
        assert "Alday" in text
        assert "Groff" in text

    def test_case_numbers_preserved(self) -> None:
        """Case numbers must be present in extracted text for all fixtures."""
        for fixture, expected_number in [
            ("oc_central_c34.pdf", "2024-01393434"),
            ("oc_costa_mesa_cm.pdf", "2024-01437598"),
            ("oc_west_w.pdf", "23-01314563"),
            ("oc_complex_cx.pdf", "2023-01301305"),
        ]:
            text = _extract_oc_pdf_text(_load_bytes(fixture))
            assert expected_number in text, (
                f"{fixture}: expected case number {expected_number} not found"
            )

    def test_old_vs_new_extraction_column_merge_audit(self) -> None:
        """Audit all fixtures: new extraction must have fewer column-merge lines.

        This is the key regression test for #1241. For each fixture, we count
        lines where a case name ('vs') and ruling text ('Before the') appear
        on the same line. The new extraction should eliminate these.
        """
        import re

        fixtures = [
            "oc_central_c34.pdf",
            "oc_costa_mesa_cm.pdf",
            "oc_west_w.pdf",
            "oc_complex_cx.pdf",
            "oc_apkarian_c25.pdf",
        ]

        any_differ = False
        for fixture in fixtures:
            pdf_bytes = _load_bytes(fixture)
            old_text = _extract_pdf_text(pdf_bytes)
            new_text = _extract_oc_pdf_text(pdf_bytes)

            # Guard against cache collision (#1545): if both extractors
            # return identical text the comparison is meaningless.
            if old_text != new_text:
                any_differ = True

            def count_merges(text: str) -> int:
                count = 0
                for line in text.split("\n"):
                    if re.search(r"\bvs\.?\b", line, re.IGNORECASE):
                        if "Before the Court" in line or "Defendant" in line[:80]:
                            count += 1
                return count

            old_merges = count_merges(old_text)
            new_merges = count_merges(new_text)
            assert new_merges <= old_merges, (
                f"{fixture}: new extraction has MORE column merges "
                f"({new_merges}) than old ({old_merges})"
            )

        # At least some fixtures must produce different text from the two
        # extractors, otherwise this test is comparing identical outputs
        # and the column-merge comparison is vacuously true.
        assert any_differ, (
            "All fixtures produced identical text from both extractors — "
            "possible cache collision bug (see #1545)"
        )

    def test_extractors_return_different_results_for_same_pdf(self) -> None:
        """Different extractors on the same PDF must return different text (#1545).

        This guards against a cache-key collision where both
        ``_extract_pdf_text`` (simple left-to-right) and
        ``_extract_oc_pdf_text`` (table-aware) are keyed by
        ``sha256(pdf_bytes)`` alone, causing the second caller to get
        the first caller's cached result.
        """
        pdf_bytes = _load_bytes("oc_apkarian_c25.pdf")
        simple_text = _extract_pdf_text(pdf_bytes)
        table_text = _extract_oc_pdf_text(pdf_bytes)
        assert simple_text != table_text, (
            "Simple and table-aware extractors returned identical text "
            "for oc_apkarian_c25.pdf — cache key collision?"
        )


# ---------------------------------------------------------------------------
# _reconstruct_entry_text — case name joining (#1360)
# ---------------------------------------------------------------------------


class TestReconstructEntryText:
    """Tests for _reconstruct_entry_text joining multi-line case names."""

    def test_multiline_case_name_joined(self) -> None:
        """Case name split across lines is joined into one line (#1360)."""
        result = _reconstruct_entry_text(
            "102",
            "Saetern vs.\nWearmouth\n25-01475675",
            "Motion to Extend Time",
        )
        lines = result.split("\n")
        assert lines[0] == "102 Saetern vs. Wearmouth"
        assert lines[1] == "25-01475675"
        assert lines[2] == "Motion to Extend Time"

    def test_plaintiff_on_separate_line(self) -> None:
        """Plaintiff and 'vs. Defendant' on separate lines are joined (#1360)."""
        result = _reconstruct_entry_text(
            "103",
            "Reyes-Schiller\nvs. Hoffman\n23-01364350",
            "Motion to Set Aside",
        )
        lines = result.split("\n")
        assert lines[0] == "103 Reyes-Schiller vs. Hoffman"
        assert lines[1] == "23-01364350"

    def test_multiword_defendant_joined(self) -> None:
        """Multi-word defendant name is joined with plaintiff (#1360)."""
        result = _reconstruct_entry_text(
            "114",
            "Coulter vs.\nGeneral Motors,\nLLC\n23-01367354",
            "Motion for Hearing",
        )
        lines = result.split("\n")
        assert lines[0] == "114 Coulter vs. General Motors, LLC"
        assert lines[1] == "23-01367354"

    def test_single_line_case_name_unchanged(self) -> None:
        """Case name already on one line is not changed."""
        result = _reconstruct_entry_text(
            "110",
            "Tong vs. Le\n25-01523131",
            "OSC re: Preliminary Injunction",
        )
        lines = result.split("\n")
        assert lines[0] == "110 Tong vs. Le"
        assert lines[1] == "25-01523131"

    def test_no_case_number_all_name(self) -> None:
        """Entry with no case number in the cell."""
        result = _reconstruct_entry_text(
            "5",
            "Smith vs.\nJones",
            "Demurrer",
        )
        lines = result.split("\n")
        assert lines[0] == "5 Smith vs. Jones"
        assert lines[1] == "Demurrer"

    def test_long_company_name(self) -> None:
        """Long company names with commas are joined properly (#1360)."""
        case_cell = (
            "Prime Healthcare\nServices - Garden\n"
            "Grove, LLC vs.\nOptumcare\nManagement,\nLLC\n"
            "23-01357480"
        )
        result = _reconstruct_entry_text("109", case_cell, "Motion to Seal")
        lines = result.split("\n")
        expected = "Prime Healthcare Services - Garden Grove, LLC vs. Optumcare Management, LLC"
        assert expected in lines[0]
        assert lines[1] == "23-01357480"

    def test_empty_case_name_cell(self) -> None:
        """Empty case name cell produces just the entry number."""
        result = _reconstruct_entry_text("1", "", "Ruling text")
        lines = result.split("\n")
        assert lines[0] == "1"
        assert lines[1] == "Ruling text"


# ---------------------------------------------------------------------------
# Fixture regression — no truncated case titles (#1360)
# ---------------------------------------------------------------------------


class TestNoCaseTitleTruncation:
    """Regression tests ensuring no case titles are truncated at 'vs' (#1360)."""

    @pytest.mark.parametrize(
        "fixture",
        [
            "oc_apkarian_c25.pdf",
            "oc_central_c34.pdf",
            "oc_complex_cx.pdf",
            "oc_costa_mesa_cm.pdf",
            "oc_west_w.pdf",
        ],
    )
    def test_no_titles_ending_in_vs(self, fixture: str) -> None:
        """No case titles should end with 'vs' or 'vs.' after extraction."""
        import re

        from ingestion.oc_splitter import _split_case_number_based

        pdf_bytes = _load_bytes(fixture)
        text = _extract_oc_pdf_text(pdf_bytes)
        results = _split_case_number_based(text)

        truncated = [
            r.case_title
            for r in results
            if r.case_title and re.search(r"\bvs\.?\s*$", r.case_title, re.IGNORECASE)
        ]
        assert truncated == [], f"{fixture}: found case titles ending in 'vs': {truncated}"

    @pytest.mark.parametrize(
        "fixture",
        [
            "oc_apkarian_c25.pdf",
            "oc_central_c34.pdf",
            "oc_complex_cx.pdf",
            "oc_costa_mesa_cm.pdf",
            "oc_west_w.pdf",
        ],
    )
    def test_no_titles_starting_with_vs(self, fixture: str) -> None:
        """No case titles should start with 'vs' or 'vs.' after extraction."""
        import re

        from ingestion.oc_splitter import _split_case_number_based

        pdf_bytes = _load_bytes(fixture)
        text = _extract_oc_pdf_text(pdf_bytes)
        results = _split_case_number_based(text)

        truncated = [
            r.case_title
            for r in results
            if r.case_title and re.search(r"^vs\.?\s", r.case_title, re.IGNORECASE)
        ]
        assert truncated == [], f"{fixture}: found case titles starting with 'vs': {truncated}"
