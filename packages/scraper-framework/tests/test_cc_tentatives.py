"""Tests for Contra Costa County tentative rulings scraper.

Fixtures captured from live site 2026-03-11:
  cc_index_page.html   — index page with 404 PDF links across 11 departments
  cc_dept16_031126.pdf — Dept 16, Judge Benjamin T Reyes II (civil, 506KB)
  cc_dept14_031026.pdf — Dept 14, Judge Kirk Athanasiou (civil/Richmond, 427KB)
  cc_dept30_031626.pdf — Dept 30, Judge Virginia M George (probate, 398KB)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.cc_tentatives import (
    BASE_URL,
    INDEX_URL,
    CCTentativeRulingsScraper,
    _cc_courthouse,
    _cc_extract_links,
    _cc_extract_motion_type,
    _cc_extract_outcome,
    _cc_hearing_date_from_filename,
    _cc_hearing_date_from_pdf,
    _cc_judge_from_pdf,
)
from courts.ca.cc_tentatives import default_config as cc_default_config
from courts.ca.pdf_link_scraper import _extract_pdf_text

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# _cc_extract_links — against real CC index page
# ---------------------------------------------------------------------------


def test_cc_extract_links_count() -> None:
    html = _load_html("cc_index_page.html")
    links = _cc_extract_links(html, BASE_URL)
    # Should have many PDF links (404 on the live page, minus temp files)
    assert len(links) > 300


def test_cc_extract_links_absolute_urls() -> None:
    html = _load_html("cc_index_page.html")
    links = _cc_extract_links(html, BASE_URL)
    for url, _, _, _ in links:
        assert url.startswith("http"), f"Expected absolute URL, got {url!r}"
        assert ".pdf" in url.lower()


def test_cc_extract_links_no_backslashes() -> None:
    """All URLs should have forward slashes after normalization."""
    html = _load_html("cc_index_page.html")
    links = _cc_extract_links(html, BASE_URL)
    for url, _, _, _ in links:
        assert "\\" not in url, f"URL still has backslashes: {url!r}"


def test_cc_extract_links_no_duplicates() -> None:
    html = _load_html("cc_index_page.html")
    links = _cc_extract_links(html, BASE_URL)
    urls = [u for u, _, _, _ in links]
    assert len(urls) == len(set(urls))


def test_cc_extract_links_no_temp_files() -> None:
    """Temp files like ~$_091525.pdf should be filtered out."""
    html = _load_html("cc_index_page.html")
    links = _cc_extract_links(html, BASE_URL)
    for url, _, _, _ in links:
        assert "~$" not in url, f"Temp file not filtered: {url!r}"


def test_cc_extract_links_department_extraction() -> None:
    html = _load_html("cc_index_page.html")
    links = _cc_extract_links(html, BASE_URL)
    # Should have links with department info
    depts_found = {dept for _, _, dept, _ in links if dept}
    assert "16" in depts_found
    assert "14" in depts_found
    assert "30" in depts_found
    assert "38" in depts_found


def test_cc_extract_links_judge_extraction() -> None:
    html = _load_html("cc_index_page.html")
    links = _cc_extract_links(html, BASE_URL)
    judges_found = {judge for _, _, _, judge in links if judge}
    assert "Reyes" in judges_found
    assert "Athanasiou" in judges_found


def test_cc_extract_links_probate_judge_no_suffix() -> None:
    """Probate judges should have '(Probate)' stripped from their name."""
    html = _load_html("cc_index_page.html")
    links = _cc_extract_links(html, BASE_URL)
    for _, _, dept, judge in links:
        if dept == "30" and judge:
            assert "(Probate)" not in judge
            assert judge == "George"
            break
    else:
        pytest.fail("No Dept 30 link found")


def test_cc_extract_links_first_link_structure() -> None:
    html = _load_html("cc_index_page.html")
    links = _cc_extract_links(html, BASE_URL)
    url, text, dept, judge = links[0]
    assert "retired.cc-courts.org" in url
    assert dept is not None
    assert judge is not None


# ---------------------------------------------------------------------------
# _cc_hearing_date_from_filename — unit tests
# ---------------------------------------------------------------------------


def test_cc_hearing_date_standard() -> None:
    assert _cc_hearing_date_from_filename("16_031126.pdf") == datetime(2026, 3, 11)


def test_cc_hearing_date_december() -> None:
    assert _cc_hearing_date_from_filename("09_122225.pdf") == datetime(2025, 12, 22)


def test_cc_hearing_date_returns_none_for_invalid() -> None:
    assert _cc_hearing_date_from_filename("invalid.pdf") is None
    assert _cc_hearing_date_from_filename("") is None


def test_cc_hearing_date_returns_none_for_bad_date_values() -> None:
    """Filename with matching pattern but invalid date values (e.g. month 99)."""
    assert _cc_hearing_date_from_filename("16_993226.pdf") is None


# ---------------------------------------------------------------------------
# _cc_hearing_date_from_pdf — against real PDF text
# ---------------------------------------------------------------------------


def test_cc_hearing_date_civil_pdf() -> None:
    text = _extract_pdf_text(_load_bytes("cc_dept16_031126.pdf"))
    dt = _cc_hearing_date_from_pdf(text)
    assert dt == datetime(2026, 3, 11)


def test_cc_hearing_date_richmond_pdf() -> None:
    text = _extract_pdf_text(_load_bytes("cc_dept14_031026.pdf"))
    dt = _cc_hearing_date_from_pdf(text)
    assert dt == datetime(2026, 3, 10)


def test_cc_hearing_date_probate_pdf() -> None:
    text = _extract_pdf_text(_load_bytes("cc_dept30_031626.pdf"))
    dt = _cc_hearing_date_from_pdf(text)
    assert dt == datetime(2026, 3, 16)


def test_cc_hearing_date_from_pdf_invalid_civil_date() -> None:
    """Civil-format date string that doesn't parse should fall through."""
    assert _cc_hearing_date_from_pdf("HEARING DATE: 99/99/9999") is None


def test_cc_hearing_date_from_pdf_no_date() -> None:
    """Text with no date patterns at all returns None."""
    assert _cc_hearing_date_from_pdf("No date information here.") is None


def test_cc_hearing_date_from_pdf_invalid_probate_date() -> None:
    """Probate-format header with invalid date values."""
    assert _cc_hearing_date_from_pdf("COURT CALENDAR FOR NOTAMONTH 99, 9999") is None


# ---------------------------------------------------------------------------
# _cc_judge_from_pdf — against real PDF text
# ---------------------------------------------------------------------------


def test_cc_judge_dept16() -> None:
    text = _extract_pdf_text(_load_bytes("cc_dept16_031126.pdf"))
    judge = _cc_judge_from_pdf(text)
    assert judge is not None
    assert "Reyes" in judge


def test_cc_judge_dept14() -> None:
    text = _extract_pdf_text(_load_bytes("cc_dept14_031026.pdf"))
    judge = _cc_judge_from_pdf(text)
    assert judge is not None
    assert "Athanasiou" in judge


def test_cc_judge_dept30() -> None:
    text = _extract_pdf_text(_load_bytes("cc_dept30_031626.pdf"))
    judge = _cc_judge_from_pdf(text)
    assert judge is not None
    assert "George" in judge


def test_cc_judge_from_pdf_no_match() -> None:
    """Text with no JUDICIAL OFFICER line returns None."""
    assert _cc_judge_from_pdf("No officer information here.") is None


def test_cc_judge_from_pdf_mixed_case() -> None:
    """Non-uppercase judge name is returned as-is."""
    result = _cc_judge_from_pdf("JUDICIAL OFFICER: Benjamin T Reyes II")
    assert result == "Benjamin T Reyes II"


# ---------------------------------------------------------------------------
# _cc_courthouse — unit tests
# ---------------------------------------------------------------------------


def test_cc_courthouse_richmond() -> None:
    assert _cc_courthouse("14") == "Richmond Courthouse"


def test_cc_courthouse_default() -> None:
    assert _cc_courthouse("16") == "Martinez Courthouse"
    assert _cc_courthouse("30") == "Martinez Courthouse"
    assert _cc_courthouse("09") == "Martinez Courthouse"


# ---------------------------------------------------------------------------
# _cc_extract_outcome — unit tests
# ---------------------------------------------------------------------------


def test_cc_outcome_granted() -> None:
    assert _cc_extract_outcome("The Motion is granted as set forth herein.") == "Granted"


def test_cc_outcome_denied() -> None:
    assert _cc_extract_outcome("The Motion is denied.") == "Denied"


def test_cc_outcome_continued() -> None:
    assert _cc_extract_outcome("The hearing is continued to April 1, 2026.") == "Continued"


def test_cc_outcome_petition_approved() -> None:
    assert _cc_extract_outcome("Petition Approved\nProposed Order Submitted") == "Granted"


def test_cc_outcome_no_appearance() -> None:
    assert _cc_extract_outcome("No Appearance Required") == "No Appearance Required"


def test_cc_outcome_none() -> None:
    assert _cc_extract_outcome("Some unrelated text without disposition") is None


# ---------------------------------------------------------------------------
# _cc_extract_motion_type — unit tests
# ---------------------------------------------------------------------------


def test_cc_motion_type_hearing_on_motion() -> None:
    text = "*HEARING ON MOTION IN RE: LEAVE TO FILE CROSS-COMPLAINT\nFILED BY: T-MOBILE"
    result = _cc_extract_motion_type(text)
    assert result is not None
    assert "Leave To File Cross-Complaint" in result


def test_cc_motion_type_cmc() -> None:
    text = "*CASE MANAGEMENT CONFERENCE\nFILED BY:"
    result = _cc_extract_motion_type(text)
    assert result is not None
    assert "Case Management Conference" in result


def test_cc_motion_type_none() -> None:
    text = "Some text without a motion type"
    assert _cc_extract_motion_type(text) is None


def test_cc_motion_type_order_to_show_cause() -> None:
    """Test OSC motion type extraction (motion_type2 group)."""
    result = _cc_extract_motion_type("*ORDER TO SHOW CAUSE")
    assert result is not None
    assert "Order To Show Cause" in result


# ---------------------------------------------------------------------------
# Case number extraction — against real PDF text
# ---------------------------------------------------------------------------


def test_cc_case_numbers_dept16() -> None:
    """Dept 16 PDF should contain civil case numbers like C24-02490."""
    import re

    from courts.ca.cc_tentatives import _CASE_NUMBER_RE

    text = _extract_pdf_text(_load_bytes("cc_dept16_031126.pdf"))
    case_numbers = _CASE_NUMBER_RE.findall(text)
    assert len(case_numbers) > 0
    # All should match the expected format
    for cn in case_numbers:
        assert re.match(r"[CLNP]\d{2}-\d{4,5}", cn), f"Unexpected format: {cn}"


def test_cc_case_numbers_dept14() -> None:
    """Dept 14 PDF should contain limited case numbers like L23-06679."""
    from courts.ca.cc_tentatives import _CASE_NUMBER_RE

    text = _extract_pdf_text(_load_bytes("cc_dept14_031026.pdf"))
    case_numbers = _CASE_NUMBER_RE.findall(text)
    assert len(case_numbers) > 0


def test_cc_case_numbers_dept30() -> None:
    """Dept 30 probate PDF should contain probate case numbers like N25-2307."""
    from courts.ca.cc_tentatives import _CASE_NUMBER_RE

    text = _extract_pdf_text(_load_bytes("cc_dept30_031626.pdf"))
    case_numbers = _CASE_NUMBER_RE.findall(text)
    assert len(case_numbers) > 0


# ---------------------------------------------------------------------------
# Case title extraction — against real PDF text
# ---------------------------------------------------------------------------


def test_cc_case_title_dept16() -> None:
    """Dept 16 should extract a case title from CASE NAME line."""
    from courts.ca.cc_tentatives import _CASE_NAME_RE

    text = _extract_pdf_text(_load_bytes("cc_dept16_031126.pdf"))
    m = _CASE_NAME_RE.search(text)
    assert m is not None
    title = m.group("case_name").strip()
    assert len(title) > 0
    # Should contain "VS." or "V." indicating party names
    assert "VS." in title.upper() or "V." in title.upper()


def test_cc_case_title_dept30_probate() -> None:
    """Dept 30 should extract case title from probate entry."""
    from courts.ca.cc_tentatives import _PROBATE_ENTRY_RE

    text = _extract_pdf_text(_load_bytes("cc_dept30_031626.pdf"))
    m = _PROBATE_ENTRY_RE.search(text)
    assert m is not None
    title = m.group("case_name").strip()
    assert len(title) > 0


# ---------------------------------------------------------------------------
# _cc_extract_links — edge cases
# ---------------------------------------------------------------------------


def test_cc_extract_links_deduplicates_urls() -> None:
    """Duplicate href values should be deduplicated."""
    html = """<html><body>
    <a class="tentative-ruling" href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>
    <a class="tentative-ruling" href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>
    </body></html>"""
    links = _cc_extract_links(html, BASE_URL)
    assert len(links) == 1


# ---------------------------------------------------------------------------
# fetch_documents error handling
# ---------------------------------------------------------------------------


@respx.mock
def test_cc_fetch_pdf_error_skips_gracefully() -> None:
    """When a PDF download fails, the scraper should skip it and continue."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    # Minimal index page with two links to different departments
    dept14_href = "TR\\Department 14 - Judge Athanasiou\\14_031026.pdf"
    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>'
        f'<a class="tentative-ruling" href="{dept14_href}">Mar 10</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    # First PDF returns 500, second succeeds
    dept16_url = (
        "https://retired.cc-courts.org/civil/TR/Department%2016%20-%20Judge%20Reyes/16_031126.pdf"
    )
    dept14_url = "https://retired.cc-courts.org/civil/TR/Department%2014%20-%20Judge%20Athanasiou/14_031026.pdf"
    respx.get(dept16_url).mock(return_value=httpx.Response(500))
    respx.get(dept14_url).mock(
        return_value=httpx.Response(200, content=_load_bytes("cc_dept14_031026.pdf"))
    )

    docs = scraper.fetch_documents()
    # Should have 1 doc (the one that succeeded)
    assert len(docs) == 1
    assert docs[0].department == "14"


@respx.mock
def test_cc_fetch_boilerplate_pdf_skipped() -> None:
    """Boilerplate PDFs should be skipped during fetch."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    index_html = """<html><body>
    <a class="tentative-ruling" href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>
    </body></html>"""
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    # Create a fake "boilerplate" PDF (just text that triggers _is_boilerplate).
    # The _is_boilerplate method checks for very short content.
    # Use a real PDF for fetch but monkey-patch _is_boilerplate to return True.
    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    original = scraper._is_boilerplate
    scraper._is_boilerplate = lambda text: True  # type: ignore[assignment]
    try:
        docs = scraper.fetch_documents()
        assert len(docs) == 0
    finally:
        scraper._is_boilerplate = original  # type: ignore[assignment]


@respx.mock
def test_cc_fetch_pdf_text_extraction_failure() -> None:
    """When PDF text extraction fails in fetch_documents, the doc should still be included."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    index_html = """<html><body>
    <a class="tentative-ruling" href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>
    </body></html>"""
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    # Return invalid PDF content that will fail text extraction
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=b"not-a-real-pdf-content")
    )

    docs = scraper.fetch_documents()
    # Doc should still be included (text extraction failure is non-fatal in fetch)
    assert len(docs) == 1


# ---------------------------------------------------------------------------
# parse_document error handling
# ---------------------------------------------------------------------------


@respx.mock
def test_cc_parse_document_pdf_extraction_failure() -> None:
    """When PDF text extraction fails in parse_document, doc is returned unchanged."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=b"not-a-real-pdf",
        content_format="pdf",
    )
    doc.department = "16"
    doc.judge_name = "Reyes"

    parsed = scraper.parse_document(doc)
    # Should return doc unchanged since text extraction fails
    assert parsed.judge_name == "Reyes"
    assert parsed.ruling_text is None


@respx.mock
def test_cc_parse_document_no_judge_extracts_from_pdf() -> None:
    """When doc has no judge_name, parse_document extracts from PDF header."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=pdf_bytes,
        content_format="pdf",
    )
    doc.department = "16"
    doc.judge_name = None  # No judge set from URL path

    parsed = scraper.parse_document(doc)
    assert parsed.judge_name is not None
    assert "Reyes" in parsed.judge_name


@respx.mock
def test_cc_parse_document_no_hearing_date_extracts_from_pdf() -> None:
    """When doc has no hearing_date, parse_document extracts from PDF text."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=pdf_bytes,
        content_format="pdf",
    )
    doc.department = "16"
    doc.hearing_date = None  # No date set from filename

    parsed = scraper.parse_document(doc)
    assert parsed.hearing_date is not None
    assert parsed.hearing_date == datetime(2026, 3, 11)


# ---------------------------------------------------------------------------
# Full scraper integration test — using respx to mock HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_cc_full_run_mocked() -> None:
    """Test a full scraper run with mocked HTTP responses."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    # Mock the index page
    index_html = _load_html("cc_index_page.html")
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    # Mock PDF downloads — use dept 16 fixture for all PDFs
    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    docs = scraper.fetch_documents()
    assert len(docs) > 0

    # Verify department extraction
    depts = {d.department for d in docs if d.department}
    assert len(depts) > 0

    # Verify judge name extraction from URL path
    judges = {d.judge_name for d in docs if d.judge_name}
    assert len(judges) > 0


@respx.mock
def test_cc_parse_civil_document() -> None:
    """Test parsing a civil PDF (Dept 16)."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")

    doc = scraper._make_base_doc(
        source_url="https://retired.cc-courts.org/civil/TR/Department%2016%20-%20Judge%20Reyes/16_031126.pdf",
        raw_content=pdf_bytes,
        content_format="pdf",
    )
    doc.department = "16"
    doc.judge_name = "Reyes"
    doc.hearing_date = datetime(2026, 3, 11)

    parsed = scraper.parse_document(doc)

    # Judge name should be refined from PDF header
    assert parsed.judge_name is not None
    assert "Reyes" in parsed.judge_name

    # Should have case number
    assert parsed.case_number is not None

    # Should have case title
    assert parsed.case_title is not None

    # Should have hearing date
    assert parsed.hearing_date == datetime(2026, 3, 11)

    # Should have ruling text
    assert parsed.ruling_text is not None
    assert len(parsed.ruling_text) > 100


@respx.mock
def test_cc_parse_probate_document() -> None:
    """Test parsing a probate PDF (Dept 30)."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    pdf_bytes = _load_bytes("cc_dept30_031626.pdf")

    doc = scraper._make_base_doc(
        source_url="https://retired.cc-courts.org/civil/TR/Department%2030/30_031626.pdf",
        raw_content=pdf_bytes,
        content_format="pdf",
    )
    doc.department = "30"
    doc.hearing_date = datetime(2026, 3, 16)

    parsed = scraper.parse_document(doc)

    # Judge name should be extracted from PDF
    assert parsed.judge_name is not None
    assert "George" in parsed.judge_name

    # Should have case number (probate format)
    assert parsed.case_number is not None

    # Should have ruling text
    assert parsed.ruling_text is not None


@respx.mock
def test_cc_parse_richmond_document() -> None:
    """Test parsing a Richmond Dept 14 PDF."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    pdf_bytes = _load_bytes("cc_dept14_031026.pdf")

    doc = scraper._make_base_doc(
        source_url="https://retired.cc-courts.org/civil/TR/Department%2014/14_031026.pdf",
        raw_content=pdf_bytes,
        content_format="pdf",
    )
    doc.department = "14"
    doc.hearing_date = datetime(2026, 3, 10)

    parsed = scraper.parse_document(doc)

    # Judge name should be extracted from PDF
    assert parsed.judge_name is not None
    assert "Athanasiou" in parsed.judge_name

    # Should have case number
    assert parsed.case_number is not None

    # Should have ruling text
    assert parsed.ruling_text is not None


# ---------------------------------------------------------------------------
# default_config
# ---------------------------------------------------------------------------


def test_cc_default_config() -> None:
    config = cc_default_config()
    assert config.scraper_id == "ca-cc-tentatives"
    assert config.state == "CA"
    assert config.county == "Contra Costa"
    assert len(config.schedule_windows) == 2
