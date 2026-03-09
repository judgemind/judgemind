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
    _is_north_dept,
    _oc_hearing_date_from_text,
    _parse_north_case_entries,
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
