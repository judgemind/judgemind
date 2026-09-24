"""Tests for Contra Costa County tentative rulings scraper.

Fixtures captured from live site 2026-03-11:
  cc_index_page.html   — index page with 404 PDF links across 11 departments
  cc_dept16_031126.pdf — Dept 16, Judge Benjamin T Reyes II (civil, 506KB)
  cc_dept14_031026.pdf — Dept 14, Judge Kirk Athanasiou (civil/Richmond, 427KB)
  cc_dept30_031626.pdf — Dept 30, Judge Virginia M George (probate, 398KB)

Additional fixture captured from live site 2026-04-14 (#2449):
  cc_probate_multi_case_041426.pdf — Dept 30, Judge Virginia M George
    (probate, 16 entries, exercises the Csicsery/Cianci/Vincent fuzzy-match
    scenario from #2449 — three Levenshtein-close case_numbers P25-02101,
    P25-02117, P25-02118 in the same calendar).

Additional fixture captured from S3 archive 2026-04-26 (#4251):
  cc_dept38_probate_040226.pdf — Dept 38, Judge Barbara C. Hinton
    (probate, 16 entries, master calendar for APRIL 2, 2026).  Reproduces
    the dept-38 master-calendar publication cadence (capture 2026-04-26,
    hearing 2026-04-02 → -24 days, outside the civil ±14 day plausibility
    window).  The regex extraction works correctly on this format; the
    case_type-aware window in ``is_plausible_hearing_date`` is what lets
    the date survive the post-extraction guard.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from courts.ca.cc_tentatives import (
    BASE_URL,
    INDEX_URL,
    CCSplitRuling,
    CCTentativeRulingsScraper,
    _cc_courthouse,
    _cc_extract_links,
    _cc_extract_motion_type,
    _cc_extract_outcome,
    _cc_hearing_date_from_filename,
    _cc_hearing_date_from_pdf,
    _cc_judge_from_pdf,
    _cc_llm_enabled,
    _extract_calendar_header_case_numbers,
    _llm_extract_rulings,
)
from courts.ca.cc_tentatives import default_config as cc_default_config
from courts.ca.pdf_link_scraper import _extract_pdf_text

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


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


def test_cc_hearing_date_dept38_probate_multi_pdf() -> None:
    """Regression for #4251 — dept 38 multi-ruling probate calendar.

    The page-1 header is ``COURT CALENDAR FOR APRIL 2, 2026`` followed by 16
    individual ruling entries.  ``_cc_hearing_date_from_pdf`` MUST extract
    the date from the cover-page header — the 16 entries below do not
    repeat the date, so the regex must lock onto the first occurrence.

    Pre-#4251 this test would still pass (regex extraction was never broken
    for this format).  The actual #4251 bug was downstream: the
    ``is_plausible_hearing_date`` ±14-day window rejected the correctly-
    extracted date because the dept-38 master calendar publishes 30+ days
    in advance.  See ``test_extract.py`` ``TestIsPlausibleHearingDate`` for
    the case_type-aware window tests; this test guards the regex layer so a
    future regex regression is caught at the layer where it would happen.
    """
    text = _extract_pdf_text(_load_bytes("cc_dept38_probate_040226.pdf"))
    dt = _cc_hearing_date_from_pdf(text)
    assert dt == datetime(2026, 4, 2)


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
    assert _cc_extract_outcome("The Motion is granted as set forth herein.") == "granted"


def test_cc_outcome_denied() -> None:
    assert _cc_extract_outcome("The Motion is denied.") == "denied"


def test_cc_outcome_continued() -> None:
    assert _cc_extract_outcome("The hearing is continued to April 1, 2026.") == "continued"


def test_cc_outcome_sustained() -> None:
    """Sustained maps to 'granted' (demurrer sustained = motion granted)."""
    assert _cc_extract_outcome("The demurrer is sustained.") == "granted"


def test_cc_outcome_overruled() -> None:
    """Overruled maps to 'denied' (demurrer overruled = motion denied)."""
    assert _cc_extract_outcome("The demurrer is overruled.") == "denied"


def test_cc_outcome_vacated() -> None:
    """Vacated maps to 'off_calendar'."""
    assert _cc_extract_outcome("The hearing is vacated.") == "off_calendar"


def test_cc_outcome_moot() -> None:
    assert _cc_extract_outcome("The motion is moot.") == "moot"


def test_cc_outcome_petition_approved() -> None:
    assert _cc_extract_outcome("Petition Approved\nProposed Order Submitted") == "granted"


def test_cc_outcome_no_appearance() -> None:
    assert _cc_extract_outcome("No Appearance Required") == "other"


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
# parse_document guard against overwriting pre_split / LLM-extracted fields
# (#2469)
# ---------------------------------------------------------------------------


@respx.mock
def test_cc_parse_document_pre_split_preserves_fields() -> None:
    """When doc.extra['pre_split'] is True, parse_document must not overwrite fields.

    Regression test for #2469: the CC scraper's LLM path populates per-ruling
    fields on each split child doc, but parse_document() used to run regex
    extraction over the full PDF text and clobber those fields.  With the
    guard in place, pre_split docs are returned unchanged.
    """
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    # Use a real multi-case PDF so regex WOULD find different values if run.
    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=pdf_bytes,
        content_format="pdf",
    )
    # Simulate fields that the scraper-side LLM path would have populated.
    doc.extra = {"pre_split": True}
    doc.case_number = "P25-02117"
    doc.case_title = "Cianci"
    doc.ruling_text = "some ruling"
    doc.motion_type = "motion_to_approve"
    doc.outcome = "granted"
    doc.parties = [{"name": "Jane Cianci", "role": "petitioner"}]

    parsed = scraper.parse_document(doc)

    # All pre-populated fields must survive parse_document unchanged.
    assert parsed.case_number == "P25-02117"
    assert parsed.case_title == "Cianci"
    assert parsed.ruling_text == "some ruling"
    assert parsed.motion_type == "motion_to_approve"
    assert parsed.outcome == "granted"
    assert parsed.parties == [{"name": "Jane Cianci", "role": "petitioner"}]
    # And the extra flag is still set so downstream stages know the doc
    # came from the pre_split path.
    assert parsed.extra.get("pre_split") is True


@respx.mock
def test_cc_parse_document_llm_extracted_preserves_fields() -> None:
    """When doc.extra['_llm_extracted'] is True, parse_document must not overwrite fields.

    Belt-and-suspenders companion to the pre_split guard — LA's parse_document
    guards on ``_llm_extracted`` rather than ``pre_split``, so CC guards on
    both to stay consistent with either upstream convention (#2469).
    """
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    doc = scraper._make_base_doc(
        source_url="https://example.com/test.pdf",
        raw_content=pdf_bytes,
        content_format="pdf",
    )
    doc.extra = {"_llm_extracted": True}
    doc.case_number = "P25-02117"
    doc.case_title = "Cianci"
    doc.ruling_text = "some ruling"

    parsed = scraper.parse_document(doc)

    assert parsed.case_number == "P25-02117"
    assert parsed.case_title == "Cianci"
    assert parsed.ruling_text == "some ruling"
    assert parsed.extra.get("_llm_extracted") is True


# ---------------------------------------------------------------------------
# Full scraper integration test — using respx to mock HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_cc_full_run_mocked() -> None:
    """Test a full scraper run with mocked HTTP responses.

    Uses a mini index page fixture (3 departments, 9 links) instead of the
    full 404-link page.  This reduces PDF download+parse iterations from 404
    to 9 while still covering civil (Dept 16), Richmond (Dept 14), and
    probate (Dept 30) code paths.  See #1219.
    """
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    # Mock the index page — use mini fixture with 3 representative departments
    index_html = _load_html("cc_index_page_mini.html")
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    # Mock PDF downloads — use dept 16 fixture for all PDFs
    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    docs = scraper.fetch_documents()
    # CC scraper fetches most-recent PDF per department: 3 depts -> 3 docs
    assert len(docs) == 3

    # Verify department extraction — all 3 kept departments should appear
    depts = {d.department for d in docs if d.department}
    assert "14" in depts
    assert "16" in depts
    assert "30" in depts

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


# ---------------------------------------------------------------------------
# LLM extraction feature flag (#2053)
# ---------------------------------------------------------------------------


def test_cc_llm_enabled_default_false() -> None:
    """LLM extraction should be disabled by default."""
    with patch.dict("os.environ", {}, clear=False):
        # Remove the env var if it exists
        import os

        os.environ.pop("ENABLE_CC_LLM_EXTRACTION", None)
        assert _cc_llm_enabled() is False


def test_cc_llm_enabled_true() -> None:
    """LLM extraction enabled when env var is set."""
    with patch.dict("os.environ", {"ENABLE_CC_LLM_EXTRACTION": "1"}):
        assert _cc_llm_enabled() is True

    with patch.dict("os.environ", {"ENABLE_CC_LLM_EXTRACTION": "true"}):
        assert _cc_llm_enabled() is True

    with patch.dict("os.environ", {"ENABLE_CC_LLM_EXTRACTION": "yes"}):
        assert _cc_llm_enabled() is True


def test_cc_llm_enabled_false_for_invalid() -> None:
    """LLM extraction disabled for non-truthy values."""
    with patch.dict("os.environ", {"ENABLE_CC_LLM_EXTRACTION": "0"}):
        assert _cc_llm_enabled() is False

    with patch.dict("os.environ", {"ENABLE_CC_LLM_EXTRACTION": "no"}):
        assert _cc_llm_enabled() is False


# ---------------------------------------------------------------------------
# _cc_llm_extract_rulings — unit tests with mocked LLM (#2053)
# ---------------------------------------------------------------------------

# Sample LLM responses for testing
_CIVIL_LLM_RESPONSE = (
    '{"extracted_judge_name": "Kirk Athanasiou",'
    '"hearing_date": "2026-03-10",'
    '"department": "14",'
    '"rulings": ['
    '{"line_number": 1,'
    '"extracted_case_number": "L23-06679",'
    '"extracted_case_title": "Discover Bank v. Gerald Gilchrist",'
    '"case_type": "civil",'
    '"outcome": "other",'
    '"motion_type": "motion_to_be_relieved_as_counsel",'
    '"ruling_text": "The motion is taken off calendar.",'
    '"extracted_parties": ['
    '{"name": "Discover Bank", "role": "plaintiff", "confidence": "high"},'
    '{"name": "Gerald Gilchrist", "role": "defendant", "confidence": "high"}'
    "]},"
    '{"line_number": 2,'
    '"extracted_case_number": "L24-02704",'
    '"extracted_case_title": "LVNV Funding LLC v. Nicole Munoz",'
    '"case_type": "civil",'
    '"outcome": "granted",'
    '"motion_type": "motion_to_vacate",'
    '"ruling_text": "The motion is granted.",'
    '"extracted_parties": ['
    '{"name": "LVNV Funding LLC", "role": "plaintiff", "confidence": "high"},'
    '{"name": "Nicole Munoz", "role": "defendant", "confidence": "high"}'
    "]}"
    "]}"
)

_PROBATE_LLM_RESPONSE = (
    '{"extracted_judge_name": "Virginia M. George",'
    '"hearing_date": "2026-03-16",'
    '"department": "30",'
    '"rulings": ['
    '{"line_number": 1,'
    '"extracted_case_number": "N25-2307",'
    '"extracted_case_title": "In the Matter of: Ajay Bhalla",'
    '"case_type": "probate",'
    '"outcome": "granted",'
    '"motion_type": "petition",'
    '"ruling_text": "Petition Approved. Proposed Order Submitted.",'
    '"extracted_parties": ['
    '{"name": "Ajay Bhalla", "role": "subject", "confidence": "high"}'
    "]}"
    "]}"
)


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_civil(mock_call_llm: MagicMock) -> None:
    """Happy path: civil format returns list of CCSplitRuling."""
    mock_response = MagicMock()
    mock_response.text = _CIVIL_LLM_RESPONSE
    mock_response.input_tokens = 1000
    mock_response.output_tokens = 500
    mock_call_llm.return_value = mock_response

    result = _llm_extract_rulings("Some PDF text content")
    assert result is not None
    assert len(result) == 2

    # First ruling
    assert result[0].case_number == "L23-06679"
    assert result[0].case_title == "Discover Bank v. Gerald Gilchrist"
    assert result[0].outcome == "other"
    assert result[0].motion_type == "motion_to_be_relieved_as_counsel"
    assert result[0].ruling_text == "The motion is taken off calendar."
    assert len(result[0].parties) == 2
    assert result[0].parties[0]["name"] == "Discover Bank"
    assert result[0].parties[0]["role"] == "plaintiff"

    # Second ruling
    assert result[1].case_number == "L24-02704"
    assert result[1].outcome == "granted"


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_probate(mock_call_llm: MagicMock) -> None:
    """Happy path: probate format returns list of CCSplitRuling."""
    mock_response = MagicMock()
    mock_response.text = _PROBATE_LLM_RESPONSE
    mock_response.input_tokens = 800
    mock_response.output_tokens = 300
    mock_call_llm.return_value = mock_response

    result = _llm_extract_rulings("Some probate PDF text")
    assert result is not None
    assert len(result) == 1
    assert result[0].case_number == "N25-2307"
    assert result[0].case_title == "In the Matter of: Ajay Bhalla"
    assert result[0].case_type == "probate"
    assert result[0].outcome == "granted"
    assert len(result[0].parties) == 1
    assert result[0].parties[0]["role"] == "subject"


# ---------------------------------------------------------------------------
# Multi-case probate regression (#2449) — exercises the Csicsery/Cianci/Vincent
# fuzzy-match scenario against a real PDF fixture.
#
# Three calendar rows in this PDF have Levenshtein-close case_numbers:
#   P25-02101 — Csicsery Family Trust
#   P25-02117 — Cianci Family 1997 Revocable Trust
#   P25-02118 — George R. Vincent and Christie J. Vincent Revocable Trust
# Before the #2467/#2468 fixes, these three rulings collapsed onto a single
# P25-02101 row with the Vincent title. This test locks in that the
# _llm_extract_rulings adapter layer preserves per-row alignment of
# (case_number, case_title, ruling_text) verbatim from the LLM response.
# ---------------------------------------------------------------------------


# Mock LLM response for cc_probate_multi_case_041426.pdf. Each entry is the
# ground truth from a calendar row in the source PDF (entries numbered
# 1..16; the PDF has 16 rulings across 15 distinct case numbers, because
# P24-01462 has two rulings — modeled here as line_number 8 and 9 with
# distinct ruling_text). ruling_text is trimmed to the first line for
# readability; this is sufficient to prove alignment because it is unique
# per row.
def _probate_ruling(
    *,
    line_number: int,
    case_number: str,
    case_title: str,
    outcome: str,
    motion_type: str,
    ruling_text: str,
    party_name: str,
) -> dict[str, object]:
    return {
        "line_number": line_number,
        "extracted_case_number": case_number,
        "extracted_case_title": case_title,
        "case_type": "probate",
        "outcome": outcome,
        "motion_type": motion_type,
        "ruling_text": ruling_text,
        "extracted_parties": [
            {"name": party_name, "role": "subject", "confidence": "high"},
        ],
    }


_MULTI_CASE_PROBATE_LLM_RESPONSE = json.dumps(
    {
        "extracted_judge_name": "Virginia M. George",
        "hearing_date": "2026-04-14",
        "department": "30",
        "rulings": [
            _probate_ruling(
                line_number=1,
                case_number="MSP12-01412",
                case_title="Bobbye M. Hickey Family Trust",
                outcome="granted",
                motion_type="petition",
                ruling_text=("Petition Approved. Proposed Order Submitted. No Appearance Required"),
                party_name="Bobbye M. Hickey Family Trust",
            ),
            _probate_ruling(
                line_number=2,
                case_number="MSP20-00800",
                case_title="Re The Sophia Acevedo Minor's Stlmnt Trust",
                outcome="other",
                motion_type="petition",
                ruling_text=("Need: 1. Proof of mailing to Dept. of Health Care Services"),
                party_name="Sophia Acevedo Minor's Settlement Trust",
            ),
            _probate_ruling(
                line_number=3,
                case_number="MSP21-00214",
                case_title="Estate of Judith E. Pinson",
                outcome="other",
                motion_type="petition",
                ruling_text="Petitioner, sister, still must do the following:",
                party_name="Judith E. Pinson",
            ),
            _probate_ruling(
                line_number=4,
                case_number="P22-01450",
                case_title="Estate of: Harold Hopkins",
                outcome="other",
                motion_type="petition",
                ruling_text=("Need appearances to report status, including mediation"),
                party_name="Harold Hopkins",
            ),
            _probate_ruling(
                line_number=5,
                case_number="P23-01809",
                case_title="Estate of: Eugene Albright",
                outcome="other",
                motion_type="petition",
                ruling_text=("Need: 1. Proof of mailing to all persons entitled to receive notice"),
                party_name="Eugene Albright",
            ),
            _probate_ruling(
                line_number=6,
                case_number="P23-02214",
                case_title=(
                    "Matter of: The Crimmins Family Trust, Dated April 5, 2000, As Amended"
                ),
                outcome="other",
                motion_type="petition",
                ruling_text=("Need appearances to report status, including mediation ordered"),
                party_name="Crimmins Family Trust",
            ),
            _probate_ruling(
                line_number=7,
                case_number="P24-00491",
                case_title="Estate of: Floda Dunn",
                outcome="other",
                motion_type="petition",
                ruling_text=("Need: 1. Appearances 2. Verified declaration by petitioner"),
                party_name="Floda Dunn",
            ),
            _probate_ruling(
                line_number=8,
                case_number="P24-01462",
                case_title=("Matter of: The Voss Family Trust Dated January 6, 1996, As Amended"),
                outcome="other",
                motion_type="motion",
                ruling_text="Need appearances",
                party_name="Voss Family Trust",
            ),
            _probate_ruling(
                line_number=9,
                case_number="P24-01462",
                case_title=("Matter of: The Voss Family Trust Dated January 6, 1996, As Amended"),
                outcome="other",
                motion_type="petition",
                ruling_text="Drop. Note: Request for Dismissal entered 2-13-2026.",
                party_name="Voss Family Trust",
            ),
            _probate_ruling(
                line_number=10,
                case_number="P24-01754",
                case_title="Estate of: Margaret Bernard",
                outcome="other",
                motion_type="petition",
                ruling_text=("Petitioner still must do the following: 1. Appear at the hearing"),
                party_name="Margaret Bernard",
            ),
            _probate_ruling(
                line_number=11,
                case_number="P25-00249",
                case_title=("Matter of: The Alan R Pufahl Revocable Trust Dated June 1, 2006"),
                outcome="other",
                motion_type="petition",
                ruling_text="Need appearances to report status",
                party_name="Alan R Pufahl Revocable Trust",
            ),
            _probate_ruling(
                line_number=12,
                case_number="P25-01473",
                case_title=(
                    "Matter of: Sue Lin Bypass Trust Dated March 10, 2007 "
                    "and The Chin-Chu Lin Survivor's Trust "
                    "Dated March 10, 2007"
                ),
                outcome="other",
                motion_type="motion",
                ruling_text=(
                    "Drop. Note: Request to Withdraw Motion for Joinder and Order filed 12-3-2025."
                ),
                party_name="Sue Lin Bypass Trust",
            ),
            _probate_ruling(
                line_number=13,
                case_number="P25-02101",
                case_title=("Matter of: The Csicsery Family Trust Dated April 24, 1992"),
                outcome="other",
                motion_type="petition",
                ruling_text=(
                    "Need: 1. Appearances 2. Proof of service in the manner provided in CCP"
                ),
                party_name="Csicsery Family Trust",
            ),
            _probate_ruling(
                line_number=14,
                case_number="P25-02117",
                case_title=(
                    "Matter of: Cianci Family 1997 Revocable Trust Agreement "
                    "As Amended and Restated"
                ),
                outcome="other",
                motion_type="petition",
                ruling_text=(
                    "Need: 1. Appearances 2. Proof of service in the manner provided in CCP"
                ),
                party_name="Cianci Family 1997 Revocable Trust",
            ),
            _probate_ruling(
                line_number=15,
                case_number="P25-02118",
                case_title=(
                    "Matter of: The George R. Vincent and "
                    "Christie J. Vincent Revocable Trust, "
                    "Under Declaration Dated March 2, 1997, "
                    "As Amended and Restated on November 4, 2011"
                ),
                outcome="granted",
                motion_type="petition",
                ruling_text=("Petition Approved. Proposed Order Submitted. No Appearance Required"),
                party_name=("George R. Vincent and Christie J. Vincent Revocable Trust"),
            ),
            _probate_ruling(
                line_number=16,
                case_number="P25-01628",
                case_title="Estate of: Razia Alam",
                outcome="other",
                motion_type="petition",
                ruling_text="Need appearances to report status",
                party_name="Razia Alam",
            ),
        ],
    }
)


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_multi_case_probate_alignment(
    mock_call_llm: MagicMock,
) -> None:
    """Regression #2449: multi-case probate PDF — per-row field alignment.

    Exercises the scenario from the original bug: three Levenshtein-close
    case_numbers (P25-02101 Csicsery, P25-02117 Cianci, P25-02118 Vincent)
    appear in the same calendar, along with 13 other rows. Each row's
    (case_number, case_title, ruling_text) tuple must be preserved verbatim
    from the LLM response — no collapse, no cross-row contamination.

    This test uses the real PDF fixture `cc_probate_multi_case_041426.pdf`
    (Dept 30, Judge Virginia M George, 2026-04-14) as the pdf_text input
    to `_llm_extract_rulings`, with a mocked LLM that returns the ground
    truth transcription of all 16 rows. Asserts per-row alignment for every
    entry plus the specific three-way fuzzy-match scenario that caused the
    original #2449 bug.
    """
    mock_response = MagicMock()
    mock_response.text = _MULTI_CASE_PROBATE_LLM_RESPONSE
    mock_response.input_tokens = 4000
    mock_response.output_tokens = 2500
    mock_call_llm.return_value = mock_response

    pdf_bytes = _load_bytes("cc_probate_multi_case_041426.pdf")
    pdf_text = _extract_pdf_text(pdf_bytes)
    assert pdf_text, "PDF text extraction must not be empty"

    result = _llm_extract_rulings(pdf_text)
    assert result is not None, "LLM extraction must succeed"
    assert len(result) == 16, f"expected 16 rulings from 16-entry calendar, got {len(result)}"

    # Index by ruling_index (1-based, matches line_number in the LLM JSON).
    # Each case_number appears exactly once in this PDF except P24-01462
    # which has two rulings (entries 8A/8B in the PDF) — both titled
    # Voss Family Trust. The bug's core regression is that per-row fields
    # must stay aligned; cross-row checks happen via Hickey, Csicsery,
    # Cianci, and Vincent below.
    by_index = {r.ruling_index: r for r in result}

    # Row 1 — Hickey (this is the one whose ruling text got misrouted in #2449).
    hickey = by_index[1]
    assert hickey.case_number == "MSP12-01412"
    assert hickey.case_title == "Bobbye M. Hickey Family Trust"
    assert "Petition Approved" in hickey.ruling_text

    # Row 13 — Csicsery (P25-02101). Before #2467, this row's title got
    # overwritten to "Vincent Trust" due to fuzzy collapse + title-clobber.
    csicsery = by_index[13]
    assert csicsery.case_number == "P25-02101"
    assert "Csicsery" in csicsery.case_title, (
        f"P25-02101 must be Csicsery, got: {csicsery.case_title!r}"
    )
    assert "Vincent" not in csicsery.case_title, (
        f"P25-02101 must NOT be titled Vincent (#2449 regression): {csicsery.case_title!r}"
    )
    assert "Appearances" in csicsery.ruling_text
    assert "Petition Approved" not in csicsery.ruling_text

    # Row 14 — Cianci (P25-02117). Before #2467, this row collapsed onto
    # P25-02101 via Levenshtein-distance-2 fuzzy match.
    cianci = by_index[14]
    assert cianci.case_number == "P25-02117"
    assert "Cianci" in cianci.case_title
    assert "Csicsery" not in cianci.case_title
    assert "Vincent" not in cianci.case_title

    # Row 15 — Vincent (P25-02118). This is the row that donated its
    # "Petition Approved" text to P25-02101 in the original bug. Its
    # ruling_text must stay on P25-02118, not leak to P25-02101.
    vincent = by_index[15]
    assert vincent.case_number == "P25-02118"
    assert "Vincent" in vincent.case_title
    assert "Csicsery" not in vincent.case_title
    assert "Petition Approved" in vincent.ruling_text

    # All three Levenshtein-close case_numbers must stay distinct.
    csicsery_cianci_vincent = {
        csicsery.case_number,
        cianci.case_number,
        vincent.case_number,
    }
    assert csicsery_cianci_vincent == {
        "P25-02101",
        "P25-02117",
        "P25-02118",
    }, (
        f"#2449 regression: the three Levenshtein-close probate numbers "
        f"must remain distinct — got {csicsery_cianci_vincent!r}"
    )

    # Global invariant: every row has a non-empty case_number, case_title,
    # and ruling_text, and ruling_index is 1..16 exactly once.
    seen_indices: set[int] = set()
    for r in result:
        assert r.case_number, f"ruling {r.ruling_index} has empty case_number"
        assert r.case_title, f"ruling {r.ruling_index} has empty case_title"
        assert r.ruling_text, f"ruling {r.ruling_index} has empty ruling_text"
        assert r.ruling_index not in seen_indices, f"duplicate ruling_index {r.ruling_index}"
        seen_indices.add(r.ruling_index)
    assert seen_indices == set(range(1, 17))


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_null_response(mock_call_llm: MagicMock) -> None:
    """LLM returns None -> function returns None."""
    mock_call_llm.return_value = None

    result = _llm_extract_rulings("Some PDF text")
    assert result is None


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_json_parse_error(mock_call_llm: MagicMock) -> None:
    """Invalid JSON in LLM response -> returns None."""
    mock_response = MagicMock()
    mock_response.text = "This is not valid JSON {{"
    mock_call_llm.return_value = mock_response

    result = _llm_extract_rulings("Some PDF text")
    assert result is None


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_bare_list(mock_call_llm: MagicMock) -> None:
    """Bare list response (not wrapped in {"rulings":[...]})."""
    bare_list = (
        '[{"line_number": 1,'
        '"extracted_case_number": "C24-02490",'
        '"extracted_case_title": "Smith v. Jones",'
        '"case_type": "civil",'
        '"outcome": "granted",'
        '"motion_type": "msj",'
        '"ruling_text": "Motion granted.",'
        '"extracted_parties": []}]'
    )
    mock_response = MagicMock()
    mock_response.text = bare_list
    mock_response.input_tokens = 500
    mock_response.output_tokens = 200
    mock_call_llm.return_value = mock_response

    result = _llm_extract_rulings("Some PDF text")
    assert result is not None
    assert len(result) == 1
    assert result[0].case_number == "C24-02490"


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_code_fences(mock_call_llm: MagicMock) -> None:
    """LLM response wrapped in markdown code fences is handled."""
    fenced = (
        '```json\n{"rulings": [{"line_number": 1,'
        ' "extracted_case_number": "L25-01552",'
        ' "ruling_text": "Granted.",'
        ' "extracted_parties": []}]}\n```'
    )
    mock_response = MagicMock()
    mock_response.text = fenced
    mock_response.input_tokens = 500
    mock_response.output_tokens = 200
    mock_call_llm.return_value = mock_response

    result = _llm_extract_rulings("Some PDF text")
    assert result is not None
    assert len(result) == 1
    assert result[0].case_number == "L25-01552"


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_empty_text(mock_call_llm: MagicMock) -> None:
    """Empty input text -> returns None without calling LLM."""
    result = _llm_extract_rulings("")
    assert result is None
    mock_call_llm.assert_not_called()

    result = _llm_extract_rulings("   ")
    assert result is None
    mock_call_llm.assert_not_called()


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_multi_issue_msj_stays_one_ruling(
    mock_call_llm: MagicMock,
) -> None:
    """#2571 regression — a multi-Issue MSJ/MSA ruling must remain ONE entry.

    Simulates the corrected LLM behaviour for the Dept 34 03/30/2026
    fixture (S3 key 406574e2..., case #20 C24-00605 VIKAS PRAKASH v.
    WILSON AQUINO).  The original bug over-split this single MSJ/MSA
    ruling into 11 per-Issue fragments with null case_number/case_title.

    This test asserts that when the LLM returns the CORRECT single-entry
    response, the extractor round-trips it unchanged — preserving the full
    ruling_text (including all 11 Issue discussions) in one CCSplitRuling.
    """
    ruling_text = (
        "The motion for summary judgment, or in the alternative, summary "
        "adjudication is DENIED as to the complaint, and GRANTED IN PART as "
        "to the cross-complaint.\n\n"
        "Issue One: Breach of Contract — The Court finds ...\n\n"
        "Issue Two: Fraud — The Court finds ...\n\n"
        "Issue Three: Breach of the Implied Covenant — ...\n\n"
        "Issue Four: Unjust Enrichment — ...\n\n"
        "Issue Five: Declaratory Relief — ...\n\n"
        "Issue Six: Affirmative Defense of Statute of Limitations — ...\n\n"
        "Issue Seven: Affirmative Defense of Laches — ...\n\n"
        "Issue Eight: Affirmative Defense of Unclean Hands — ...\n\n"
        "Issue Nine: Affirmative Defense of Waiver — ...\n\n"
        "Issue Ten: Affirmative Defense of Estoppel — ...\n\n"
        "Issue Eleven: Affirmative Defense of Failure to Mitigate — ..."
    )
    response_json = (
        '{"rulings": [{'
        '"line_number": 20,'
        '"extracted_case_number": "C24-00605",'
        '"extracted_case_title": "Vikas Prakash v. Wilson Aquino",'
        '"case_type": "civil",'
        '"outcome": "granted_in_part",'
        '"motion_type": "msj_partial",'
        '"ruling_text": ' + _json_escape(ruling_text) + ","
        '"extracted_parties": ['
        '{"name": "Vikas Prakash", "role": "plaintiff", "confidence": "high"},'
        '{"name": "Wilson Aquino", "role": "defendant", "confidence": "high"}'
        "]}]}"
    )
    mock_response = MagicMock()
    mock_response.text = response_json
    mock_response.input_tokens = 2000
    mock_response.output_tokens = 1500
    mock_call_llm.return_value = mock_response

    result = _llm_extract_rulings("Some PDF text content")

    # CRITICAL: exactly ONE ruling, not 11.
    assert result is not None
    assert len(result) == 1, (
        f"Expected 1 ruling for multi-Issue MSJ/MSA, got {len(result)} "
        "— over-splitting regression (see #2571)"
    )

    only = result[0]
    # All 11 Issue discussions must be preserved verbatim in ruling_text.
    for issue_label in (
        "Issue One",
        "Issue Two",
        "Issue Three",
        "Issue Four",
        "Issue Five",
        "Issue Six",
        "Issue Seven",
        "Issue Eight",
        "Issue Nine",
        "Issue Ten",
        "Issue Eleven",
    ):
        assert issue_label in (only.ruling_text or ""), (
            f"'{issue_label}' missing from ruling_text — over-splitting regression"
        )

    # Case metadata must be present (case_number + case_title).
    assert only.case_number == "C24-00605"
    assert only.case_title == "Vikas Prakash v. Wilson Aquino"
    # Motion type should be an MSJ-family label, not generic adjudication.
    assert only.motion_type in ("msj", "msj_partial", "motion_for_summary_adjudication")


def _json_escape(text: str) -> str:
    """Minimal JSON-string escape for inline test fixtures."""
    import json

    return json.dumps(text)


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_outcome_mapping(mock_call_llm: MagicMock) -> None:
    """Outcome values are mapped through _CC_OUTCOME_MAP."""
    response_json = (
        '{"rulings": ['
        '{"line_number": 1, "extracted_case_number": "C24-00001",'
        '"outcome": "granted_in_part", "ruling_text": "Partially granted.",'
        '"extracted_parties": []},'
        '{"line_number": 2, "extracted_case_number": "C24-00002",'
        '"outcome": "off_calendar", "ruling_text": "Off calendar.",'
        '"extracted_parties": []},'
        '{"line_number": 3, "extracted_case_number": "C24-00003",'
        '"outcome": "unknown_value", "ruling_text": "Unknown.",'
        '"extracted_parties": []}'
        "]}"
    )
    mock_response = MagicMock()
    mock_response.text = response_json
    mock_response.input_tokens = 500
    mock_response.output_tokens = 300
    mock_call_llm.return_value = mock_response

    result = _llm_extract_rulings("Some text")
    assert result is not None
    assert result[0].outcome == "granted_in_part"
    assert result[1].outcome == "off_calendar"
    # Unknown values not in the map -> None
    assert result[2].outcome is None


@patch("ingestion.llm_providers.call_llm")
def test_cc_llm_extract_rulings_invalid_party_skipped(
    mock_call_llm: MagicMock,
) -> None:
    """Parties with invalid names (too long, newlines) are skipped."""
    long_name = "A" * 201
    response_json = (
        '{"rulings": [{"line_number": 1, "extracted_case_number": "C24-00001",'
        '"ruling_text": "Granted.", "extracted_parties": ['
        '{"name": "' + long_name + '", "role": "plaintiff"},'
        '{"name": "Valid Name", "role": "defendant"}'
        "]}]}"
    )
    mock_response = MagicMock()
    mock_response.text = response_json
    mock_response.input_tokens = 500
    mock_response.output_tokens = 200
    mock_call_llm.return_value = mock_response

    result = _llm_extract_rulings("Some text")
    assert result is not None
    assert len(result[0].parties) == 1
    assert result[0].parties[0]["name"] == "Valid Name"


# ---------------------------------------------------------------------------
# Integration: fetch_documents with LLM enabled (#2053)
# ---------------------------------------------------------------------------


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_fetch_with_llm_enabled(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """When LLM is enabled, fetch_documents produces split docs with all fields."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    # Mock index page with one department
    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 14 - Judge Athanasiou\\14_031026.pdf">Mar 10</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    # Mock PDF download
    pdf_bytes = _load_bytes("cc_dept14_031026.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    # Mock LLM extraction — return 2 split rulings
    mock_extract.return_value = [
        CCSplitRuling(
            ruling_index=1,
            case_number="L23-06679",
            ruling_text="The motion is taken off calendar.",
            case_title="Discover Bank v. Gerald Gilchrist",
            case_type="civil",
            motion_type="motion_to_be_relieved_as_counsel",
            outcome="other",
            parties=[
                {"name": "Discover Bank", "role": "plaintiff"},
                {"name": "Gerald Gilchrist", "role": "defendant"},
            ],
        ),
        CCSplitRuling(
            ruling_index=2,
            case_number="L24-02704",
            ruling_text="The motion is granted.",
            case_title="LVNV Funding LLC v. Nicole Munoz",
            case_type="civil",
            motion_type="motion_to_vacate",
            outcome="granted",
            parties=[],
        ),
    ]

    docs = scraper.fetch_documents()

    # Should have 2 docs (one per ruling, not one per PDF)
    assert len(docs) == 2

    # All docs should have LLM extraction markers
    for doc in docs:
        assert doc.extra.get("_llm_extracted") is True
        assert doc.extra.get("pre_split") is True
        assert doc.department == "14"
        assert doc.courthouse == "Richmond Courthouse"

    # Check first doc fields
    assert docs[0].case_number == "L23-06679"
    assert docs[0].case_title == "Discover Bank v. Gerald Gilchrist"
    assert docs[0].ruling_text == "The motion is taken off calendar."
    assert docs[0].motion_type == "motion_to_be_relieved_as_counsel"
    assert docs[0].outcome == "other"
    assert len(docs[0].parties) == 2
    assert docs[0].extra["ruling_index"] == 1
    assert docs[0].extra["case_type"] == "civil"

    # Check second doc
    assert docs[1].case_number == "L24-02704"
    assert docs[1].outcome == "granted"
    assert docs[1].extra["ruling_index"] == 2

    # Judge name should be refined from PDF header (not URL path)
    for doc in docs:
        assert doc.judge_name is not None
        assert "Athanasiou" in doc.judge_name


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_fetch_llm_fallback_on_failure(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """When LLM extraction fails, falls back to single-doc regex path."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    # LLM extraction returns None (failure)
    mock_extract.return_value = None

    docs = scraper.fetch_documents()

    # Should fall back to single doc per PDF
    assert len(docs) == 1
    assert docs[0].department == "16"
    # Should NOT have LLM extraction markers
    assert docs[0].extra.get("_llm_extracted") is not True
    assert docs[0].extra.get("pre_split") is not True


@respx.mock
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=False)
def test_cc_fetch_llm_disabled_uses_regex(mock_llm_enabled: MagicMock) -> None:
    """When LLM is disabled, fetch_documents uses the single-doc regex path."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 14 - Judge Athanasiou\\14_031026.pdf">Mar 10</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    pdf_bytes = _load_bytes("cc_dept14_031026.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    docs = scraper.fetch_documents()

    # Should produce a single doc (not split by LLM)
    assert len(docs) == 1
    assert docs[0].department == "14"
    assert docs[0].extra.get("_llm_extracted") is not True


# ---------------------------------------------------------------------------
# Regex fallback for null LLM outcome/motion_type fields (#4029)
# ---------------------------------------------------------------------------


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_llm_split_null_outcome_fallback(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """When LLM returns outcome=None, the regex fallback populates outcome from ruling_text."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    mock_extract.return_value = [
        CCSplitRuling(
            ruling_index=1,
            case_number="C24-01512",  # valid case number in cc_dept16_031126.pdf
            ruling_text="Motion for Summary Judgment is granted.",
            case_title="Smith v. Jones",
            case_type="civil",
            motion_type="motion_for_summary_judgment",
            outcome=None,  # LLM left this None
            parties=[],
        ),
    ]

    docs = scraper.fetch_documents()

    assert len(docs) == 1
    # Regex fallback should have extracted "granted" from ruling_text
    assert docs[0].outcome == "granted"


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_llm_split_null_motion_type_fallback(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """When LLM returns motion_type=None, the regex fallback populates it from ruling_text."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    mock_extract.return_value = [
        CCSplitRuling(
            ruling_index=1,
            case_number="C24-01512",  # valid case number in cc_dept16_031126.pdf
            ruling_text="*HEARING ON MOTION TO COMPEL\nThe motion is granted.",
            case_title="Smith v. Jones",
            case_type="civil",
            motion_type=None,  # LLM left this None
            outcome="granted",
            parties=[],
        ),
    ]

    docs = scraper.fetch_documents()

    assert len(docs) == 1
    # Regex fallback should have extracted a motion type from the HEARING ON MOTION line
    assert docs[0].motion_type is not None


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_llm_split_preserves_positive_llm_classification(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """When LLM returns non-None outcome/motion_type, regex fallback must NOT overwrite."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    mock_extract.return_value = [
        CCSplitRuling(
            ruling_index=1,
            case_number="C24-01512",  # valid case number in cc_dept16_031126.pdf
            # ruling_text says "is granted" but LLM says "denied" — LLM wins
            ruling_text="*HEARING ON MOTION TO COMPEL\nThe motion is granted.",
            case_title="Smith v. Jones",
            case_type="civil",
            motion_type="motion_to_compel",
            outcome="denied",  # LLM classification
            parties=[],
        ),
    ]

    docs = scraper.fetch_documents()

    assert len(docs) == 1
    # LLM values must be preserved; regex fallback must NOT run
    assert docs[0].outcome == "denied"
    assert docs[0].motion_type == "motion_to_compel"


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_llm_split_null_fields_with_no_ruling_text(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """When LLM returns both fields None and ruling_text is empty, both remain None (no crash)."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    mock_extract.return_value = [
        CCSplitRuling(
            ruling_index=1,
            case_number="C24-01512",  # valid case number in cc_dept16_031126.pdf
            ruling_text="",  # Empty ruling text
            case_title="Smith v. Jones",
            case_type="civil",
            motion_type=None,
            outcome=None,
            parties=[],
        ),
    ]

    docs = scraper.fetch_documents()

    assert len(docs) == 1
    # Both fields remain None — no crash, no spurious match
    assert docs[0].outcome is None
    assert docs[0].motion_type is None


# ---------------------------------------------------------------------------
# Regression: probate PDFs use content-hash filenames — hearing_date from PDF header
# (#3739)
# ---------------------------------------------------------------------------


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_fetch_probate_hash_filename_hearing_date_llm_path(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """Probate PDFs use content-hash filenames (e.g. 594361f8.pdf) that do not
    match the NN_MMDDYY pattern.  With LLM enabled, each emitted doc must still
    carry hearing_date populated from the PDF calendar header, not the filename."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    # Content-hash-style href — _cc_hearing_date_from_filename returns None for this.
    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR/Department 30/594361f8.pdf">Mar 16</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    # Load probate fixture: calendar header "COURT CALENDAR FOR MARCH 16, 2026"
    pdf_bytes = _load_bytes("cc_dept30_031626.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    # Use a real case number from the cc_dept30_031626.pdf fixture calendar headers
    # so the phantom-ruling guard (#3798) does not drop it.
    mock_extract.return_value = [
        CCSplitRuling(
            ruling_index=1,
            case_number="P24-00230",
            ruling_text="The petition is granted.",
            case_title="Conservatorship of: June Stone",
            case_type="probate",
            motion_type=None,
            outcome="granted",
            parties=[],
        ),
    ]

    docs = scraper.fetch_documents()

    assert len(docs) == 1
    doc = docs[0]
    # Core regression assertion: hearing_date must be populated from PDF header
    assert doc.hearing_date == datetime(2026, 3, 16), (
        f"Expected hearing_date 2026-03-16 from PDF header, got {doc.hearing_date!r}"
    )
    # LLM path markers must be set
    assert doc.extra.get("pre_split") is True
    assert doc.extra.get("_llm_extracted") is True


@respx.mock
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=False)
def test_cc_fetch_probate_hash_filename_hearing_date_regex_path(
    mock_llm_enabled: MagicMock,
) -> None:
    """Probate PDFs use content-hash filenames.  With LLM disabled (single-doc
    regex path), hearing_date must still be populated from the PDF calendar header."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    # Content-hash-style href
    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR/Department 30/594361f8.pdf">Mar 16</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    pdf_bytes = _load_bytes("cc_dept30_031626.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    docs = scraper.fetch_documents()

    assert len(docs) == 1
    doc = docs[0]
    assert doc.hearing_date == datetime(2026, 3, 16), (
        f"Expected hearing_date 2026-03-16 from PDF header, got {doc.hearing_date!r}"
    )


# ---------------------------------------------------------------------------
# _extract_calendar_header_case_numbers — pure unit tests (#3798)
# ---------------------------------------------------------------------------


def test_cc_extract_calendar_header_case_numbers_civil() -> None:
    """Format A civil header extracts case numbers correctly."""
    text = (
        "SUPERIOR COURT OF CALIFORNIA, CONTRA COSTA COUNTY\n"
        "DEPARTMENT 16\n"
        "JUDICIAL OFFICER: BENJAMIN T REYES II\n"
        "HEARING DATE: 03/11/2026\n\n"
        "8. 9:00 AM CASE NUMBER: L25-09151\n"
        "CASE NAME: SMITH V. JONES\n"
        "*TENTATIVE RULING: Granted.\n\n"
        "9. 9:30 AM CASE NUMBER: C24-03348\n"
        "CASE NAME: DOE V. DOE\n"
        "*TENTATIVE RULING: Denied.\n"
    )
    result = _extract_calendar_header_case_numbers(text)
    assert "L25-09151" in result
    assert "C24-03348" in result


def test_cc_extract_calendar_header_case_numbers_probate() -> None:
    """Format B probate header extracts case numbers correctly."""
    text = (
        "SUPERIOR COURT OF CALIFORNIA, CONTRA COSTA COUNTY\n"
        "PR - MARTINEZ-WAKEFIELD TAYLOR COURTHOUSE\n"
        "COURT CALENDAR FOR MARCH 16, 2026\n"
        "DEPARTMENT 30\n\n"
        "7. P24-00230 CONSERVATORSHIP OF: JUNE STONE\n"
        "9:00 AM HEARING\n\n"
        "8. N25-2307 IN THE MATTER OF: AJAY BHALLA\n"
    )
    result = _extract_calendar_header_case_numbers(text)
    assert "P24-00230" in result
    assert "N25-2307" in result


def test_cc_extract_calendar_header_case_numbers_normalised_uppercase() -> None:
    """Extracted case numbers are normalised to uppercase."""
    text = "8. 9:00 AM CASE NUMBER: l25-09151\n"
    result = _extract_calendar_header_case_numbers(text)
    # After upper(), the lowercase prefix becomes uppercase
    assert "L25-09151" in result


def test_cc_extract_calendar_header_case_numbers_empty() -> None:
    """Empty text returns empty set."""
    assert _extract_calendar_header_case_numbers("") == set()


def test_cc_extract_calendar_header_case_numbers_no_headers() -> None:
    """Text with no calendar headers returns empty set."""
    result = _extract_calendar_header_case_numbers("Just some random text without any entries.")
    assert result == set()


def test_cc_extract_calendar_header_case_numbers_mixed_formats() -> None:
    """Both Format A and Format B entries are captured from the same text."""
    text = (
        "1. 9:00 AM CASE NUMBER: C23-01234\n"
        "CASE NAME: ALPHA V. BETA\n\n"
        "2. P24-00230 CONSERVATORSHIP OF: JUNE STONE\n"
    )
    result = _extract_calendar_header_case_numbers(text)
    assert "C23-01234" in result
    assert "P24-00230" in result


def test_cc_extract_calendar_header_n_prefix_on_civil_dept_pdf_is_legitimate() -> None:
    """N-prefix case on a civil-dept PDF is a legitimate calendar header entry.

    Regression for #4291.  The investigation surfaced three residual NULL
    hearing-date rulings (N25-2244, N25-2433, N26-0247) on civil-dept master
    calendar PDFs (Dept 34, 14, 32).  The issue body's hypothesis #1 was that
    these were LLM phantom-attribution rulings — case numbers mentioned in
    body text, not the calendar header.  This test falsifies that hypothesis
    on the dept-34 fixture: N25-2244 IS extracted as a top-level Format A
    civil header entry by ``_extract_calendar_header_case_numbers``.

    The actual root cause (#4291) was the CC system prompt's
    ``N##-#### -- name change / probate`` mapping biasing the LLM to return
    ``case_type='probate'`` for these civil rulings — fixed by adding the
    civil-dept context override in ``framework/prompts/contra_costa.py``.
    """
    text = _extract_pdf_text(_load_bytes("cc_dept34_033026.pdf"))

    result = _extract_calendar_header_case_numbers(text)

    # N25-2244 is calendar entry #16 in the dept-34 civil PDF — see the
    # raw text:  ``16. 9:00 AM CASE NUMBER: N25-2244``.  The phantom-ruling
    # guard would NOT drop it because it matches Format A civil header.
    assert "N25-2244" in result
    # Sanity: civil C-prefix entries on the same PDF are also extracted,
    # confirming the extraction is operating across the full calendar.
    assert "C25-02672" in result


# ---------------------------------------------------------------------------
# Phantom-ruling guard in fetch_documents (#3798)
# ---------------------------------------------------------------------------


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_phantom_ruling_dropped_civil_citation(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """Phantom ruling dropped; legitimate ruling preserved (AC#1 verify map).

    Text has one civil calendar entry for L25-09151.  LLM returns two rulings:
    one legitimate (L25-09151) and one phantom (C24-03348).  The guard must
    drop the phantom and preserve exactly one doc with case_number L25-09151.
    """
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    # Build synthetic PDF text with one civil calendar entry
    pdf_text_header = (
        "SUPERIOR COURT OF CALIFORNIA, CONTRA COSTA COUNTY\n"
        "DEPARTMENT 16\n"
        "JUDICIAL OFFICER: BENJAMIN T REYES II\n"
        "HEARING DATE: 03/11/2026\n\n"
        "8. 9:00 AM CASE NUMBER: L25-09151\n"
        "CASE NAME: SMITH V. JONES\n"
        "*TENTATIVE RULING: The motion is granted.\n"
    )

    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    # Build a minimal valid PDF wrapping the text via pdfplumber-compatible bytes
    # Simplest: use the real fixture but override text extraction via mock
    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    # LLM returns one legit ruling and one phantom
    mock_extract.return_value = [
        CCSplitRuling(
            ruling_index=1,
            case_number="L25-09151",
            ruling_text="The motion is granted.",
            case_title="Smith v. Jones",
            case_type="civil",
            motion_type="msj",
            outcome="granted",
            parties=[],
        ),
        CCSplitRuling(
            ruling_index=2,
            case_number="C24-03348",
            ruling_text="The motion is denied.",
            case_title="Plaintiff v. Defendant",
            case_type="civil",
            motion_type="msj",
            outcome="denied",
            parties=[],
        ),
    ]

    # Patch _extract_pdf_text to return our synthetic text so the guard can
    # see only L25-09151 in the calendar headers.
    with patch("courts.ca.cc_tentatives._extract_pdf_text", return_value=pdf_text_header):
        docs = scraper.fetch_documents()

    # Exactly one doc — the legitimate one
    assert len(docs) == 1, (
        f"Expected 1 doc (phantom dropped), got {len(docs)}: {[d.case_number for d in docs]}"
    )
    assert docs[0].case_number == "L25-09151"


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_phantom_ruling_legit_multi_motion_preserved(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """Multi-motion rulings with same case_number are all preserved.

    Lines 7, 8, 9 in the calendar all reference C23-02436.  Three LLM rulings
    share that case_number — all three must survive the guard (consistency with
    prompt rule 1, lines 93-95 in the task plan).
    """
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    # Three calendar entries all for C23-02436
    pdf_text_header = (
        "SUPERIOR COURT OF CALIFORNIA, CONTRA COSTA COUNTY\n"
        "DEPARTMENT 16\n"
        "JUDICIAL OFFICER: BENJAMIN T REYES II\n"
        "HEARING DATE: 03/11/2026\n\n"
        "7. 9:00 AM CASE NUMBER: C23-02436\n"
        "CASE NAME: ALPHA V. BETA\n"
        "*HEARING ON MOTION IN RE: MOTION TO COMPEL\n"
        "*TENTATIVE RULING: Granted.\n\n"
        "8. 9:15 AM CASE NUMBER: C23-02436\n"
        "CASE NAME: ALPHA V. BETA\n"
        "*HEARING ON MOTION IN RE: MOTION FOR SANCTIONS\n"
        "*TENTATIVE RULING: Denied.\n\n"
        "9. 9:30 AM CASE NUMBER: C23-02436\n"
        "CASE NAME: ALPHA V. BETA\n"
        "*HEARING ON MOTION IN RE: MOTION TO STRIKE\n"
        "*TENTATIVE RULING: Granted.\n"
    )

    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    # LLM returns three rulings all with C23-02436
    mock_extract.return_value = [
        CCSplitRuling(
            ruling_index=7,
            case_number="C23-02436",
            ruling_text="Granted.",
            case_title="Alpha v. Beta",
            case_type="civil",
            motion_type="motion_to_compel",
            outcome="granted",
            parties=[],
        ),
        CCSplitRuling(
            ruling_index=8,
            case_number="C23-02436",
            ruling_text="Denied.",
            case_title="Alpha v. Beta",
            case_type="civil",
            motion_type="motion_for_sanctions",
            outcome="denied",
            parties=[],
        ),
        CCSplitRuling(
            ruling_index=9,
            case_number="C23-02436",
            ruling_text="Granted.",
            case_title="Alpha v. Beta",
            case_type="civil",
            motion_type="motion_to_strike",
            outcome="granted",
            parties=[],
        ),
    ]

    with patch("courts.ca.cc_tentatives._extract_pdf_text", return_value=pdf_text_header):
        docs = scraper.fetch_documents()

    # All three must be preserved — same case_number in three valid calendar entries
    assert len(docs) == 3, f"Expected 3 docs (multi-motion, all legit), got {len(docs)}"
    for doc in docs:
        assert doc.case_number == "C23-02436"


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_phantom_ruling_probate_format_b(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """Probate Format B: phantom dropped, legitimate probate ruling preserved."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    pdf_text_header = (
        "SUPERIOR COURT OF CALIFORNIA, CONTRA COSTA COUNTY\n"
        "PR - MARTINEZ-WAKEFIELD TAYLOR COURTHOUSE\n"
        "COURT CALENDAR FOR MARCH 16, 2026\n"
        "DEPARTMENT 30\n\n"
        "7. P24-00230 CONSERVATORSHIP OF: JUNE STONE\n"
        "9:00 AM HEARING\n"
    )

    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 30\\30_031626.pdf">Mar 16</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    pdf_bytes = _load_bytes("cc_dept30_031626.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    # LLM returns one legit probate ruling and one phantom civil ruling
    mock_extract.return_value = [
        CCSplitRuling(
            ruling_index=7,
            case_number="P24-00230",
            ruling_text="Petition granted.",
            case_title="Conservatorship of: June Stone",
            case_type="probate",
            motion_type="petition",
            outcome="granted",
            parties=[],
        ),
        CCSplitRuling(
            ruling_index=99,
            case_number="C24-03348",
            ruling_text="Some phantom ruling.",
            case_title="Phantom v. Case",
            case_type="civil",
            motion_type="msj",
            outcome="denied",
            parties=[],
        ),
    ]

    with patch("courts.ca.cc_tentatives._extract_pdf_text", return_value=pdf_text_header):
        docs = scraper.fetch_documents()

    assert len(docs) == 1, (
        f"Expected 1 doc (phantom dropped), got {len(docs)}: {[d.case_number for d in docs]}"
    )
    assert docs[0].case_number == "P24-00230"


@respx.mock
@patch("courts.ca.cc_tentatives._llm_extract_rulings")
@patch("courts.ca.cc_tentatives._cc_llm_enabled", return_value=True)
def test_cc_phantom_ruling_null_case_number_passthrough(
    mock_llm_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """Rulings with case_number=None are appended unchanged (out of scope for guard)."""
    config = cc_default_config()
    scraper = CCTentativeRulingsScraper(config)

    # Calendar text has no case number entries (empty valid_cns set)
    pdf_text_header = (
        "SUPERIOR COURT OF CALIFORNIA, CONTRA COSTA COUNTY\n"
        "DEPARTMENT 16\n"
        "JUDICIAL OFFICER: BENJAMIN T REYES II\n"
        "HEARING DATE: 03/11/2026\n"
        "Some preamble with no calendar entries.\n"
    )

    index_html = (
        "<html><body>"
        '<a class="tentative-ruling" '
        'href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11</a>'
        "</body></html>"
    )
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=index_html))

    pdf_bytes = _load_bytes("cc_dept16_031126.pdf")
    respx.route(method="GET", url__regex=r".*\.pdf$").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )

    # LLM returns one ruling with null case_number
    mock_extract.return_value = [
        CCSplitRuling(
            ruling_index=1,
            case_number=None,
            ruling_text="Some ruling without a case number.",
            case_title=None,
            case_type=None,
            motion_type=None,
            outcome=None,
            parties=[],
        ),
    ]

    with patch("courts.ca.cc_tentatives._extract_pdf_text", return_value=pdf_text_header):
        docs = scraper.fetch_documents()

    # Null case_number ruling must pass through (guard only fires on non-null)
    assert len(docs) == 1
    assert docs[0].case_number is None


# ---------------------------------------------------------------------------
# Prompt consistency test (#3798, AC#2)
# ---------------------------------------------------------------------------


def test_contra_costa_prompt_no_plaintiff_v_defendant_literal() -> None:
    """AC#2 verify: prompt must not contain 'Plaintiff v. Defendant' literal.

    The old rule 3 used generic role words as party names.  The fix must
    replace this with explicit naming from the CASE NAME line, with a null
    fallback.  Two conditions are checked:
    1. The literal 'Plaintiff v. Defendant' (role-word placeholder) is gone.
    2. The phrase 'return null' appears in the prompt (null fallback added).
    """
    from framework.prompts.contra_costa import CONTRA_COSTA_SYSTEM_PROMPT

    # AC#2 verify condition 1: no role-word placeholder
    assert "Plaintiff v. Defendant" not in CONTRA_COSTA_SYSTEM_PROMPT, (
        "Prompt must not contain generic 'Plaintiff v. Defendant' role-word placeholder. "
        "Fix rule 3 to use actual party names from the CASE NAME line."
    )

    # AC#2 verify condition 2: null fallback added for missing/ambiguous CASE NAME
    assert "return null" in CONTRA_COSTA_SYSTEM_PROMPT, (
        "Prompt must contain 'return null' in the case_title section "
        "as a fallback when CASE NAME line is missing or ambiguous."
    )
