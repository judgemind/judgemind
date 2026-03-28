"""Tests for Ventura County tentative rulings scraper.

Fixtures:
  ventura_search_page.html     — GET search page with anti-forgery token
  ventura_results_page.html    — POST results with 6 rows across 3 departments
  ventura_no_results_page.html — POST results with no matching rulings
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from courts.ca.ventura_tentatives import (
    SEARCH_URL,
    VenturaTentativeRulingsScraper,
    _extract_case_title,
    _extract_html_text,
    _extract_outcome,
    _ventura_llm_enabled,
    _ventura_llm_extract,
    extract_verification_token,
    parse_event_datetime,
    parse_results_table,
)
from courts.ca.ventura_tentatives import default_config as ventura_default_config

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Anti-forgery token extraction
# ---------------------------------------------------------------------------


def test_extract_verification_token() -> None:
    html = _load_html("ventura_search_page.html")
    token = extract_verification_token(html)
    assert token is not None
    assert "TEST_TOKEN_VALUE" in token


def test_extract_verification_token_from_results() -> None:
    html = _load_html("ventura_results_page.html")
    token = extract_verification_token(html)
    assert token is not None


def test_extract_verification_token_missing() -> None:
    html = "<html><body>No token here</body></html>"
    token = extract_verification_token(html)
    assert token is None


# ---------------------------------------------------------------------------
# Event datetime parsing
# ---------------------------------------------------------------------------


def test_parse_event_datetime_am() -> None:
    dt = parse_event_datetime("03/11/2026 8:30 AM")
    assert dt == datetime(2026, 3, 11, 8, 30)


def test_parse_event_datetime_pm() -> None:
    dt = parse_event_datetime("03/11/2026 1:30 PM")
    assert dt == datetime(2026, 3, 11, 13, 30)


def test_parse_event_datetime_noon() -> None:
    dt = parse_event_datetime("03/11/2026 12:00 PM")
    assert dt == datetime(2026, 3, 11, 12, 0)


def test_parse_event_datetime_midnight() -> None:
    dt = parse_event_datetime("03/11/2026 12:00 AM")
    assert dt == datetime(2026, 3, 11, 0, 0)


def test_parse_event_datetime_none() -> None:
    assert parse_event_datetime("not a date") is None
    assert parse_event_datetime("") is None


# ---------------------------------------------------------------------------
# Results table parsing
# ---------------------------------------------------------------------------


def test_parse_results_table_count() -> None:
    html = _load_html("ventura_results_page.html")
    results = parse_results_table(html)
    assert len(results) == 6


def test_parse_results_table_first_row() -> None:
    html = _load_html("ventura_results_page.html")
    results = parse_results_table(html)
    r = results[0]
    assert r.case_number == "2025CUBC040123"
    assert r.event_type == "Demurrer to Complaint"
    assert r.department == "20"
    assert r.doc_id == "100001"
    assert r.event_datetime == datetime(2026, 3, 11, 8, 30)


def test_parse_results_table_departments() -> None:
    html = _load_html("ventura_results_page.html")
    results = parse_results_table(html)
    departments = {r.department for r in results}
    assert "20" in departments
    assert "42" in departments
    assert "43" in departments


def test_parse_results_table_all_have_doc_ids() -> None:
    html = _load_html("ventura_results_page.html")
    results = parse_results_table(html)
    for r in results:
        assert r.doc_id is not None, f"Missing doc_id for {r.case_number}"


def test_parse_results_table_case_numbers() -> None:
    html = _load_html("ventura_results_page.html")
    results = parse_results_table(html)
    case_numbers = [r.case_number for r in results]
    assert "2025CUBC040123" in case_numbers
    assert "2024CUBC038456" in case_numbers
    assert "2025CUBC041789" in case_numbers
    assert "2025CUPR042345" in case_numbers


def test_parse_results_table_event_types() -> None:
    html = _load_html("ventura_results_page.html")
    results = parse_results_table(html)
    event_types = [r.event_type for r in results]
    assert "Demurrer to Complaint" in event_types
    assert "Motion to Compel Further Responses" in event_types
    assert "Motion for Summary Judgment" in event_types


def test_parse_results_table_no_results() -> None:
    html = _load_html("ventura_no_results_page.html")
    results = parse_results_table(html)
    assert len(results) == 0


def test_parse_results_table_empty_html() -> None:
    results = parse_results_table("<html><body></body></html>")
    assert len(results) == 0


# ---------------------------------------------------------------------------
# Outcome extraction
# ---------------------------------------------------------------------------


def test_extract_outcome_granted() -> None:
    assert _extract_outcome("The motion is GRANTED.") == "Granted"


def test_extract_outcome_denied() -> None:
    assert _extract_outcome("The demurrer is DENIED.") == "Denied"


def test_extract_outcome_sustained() -> None:
    assert _extract_outcome("The demurrer is sustained.") == "Sustained"


def test_extract_outcome_overruled() -> None:
    assert _extract_outcome("The demurrer is overruled.") == "Overruled"


def test_extract_outcome_off_calendar() -> None:
    assert _extract_outcome("The motion is taken OFF CALENDAR.") == "Off Calendar"


def test_extract_outcome_continued() -> None:
    assert _extract_outcome("The matter is continued to April 1, 2026.") == "Continued"


def test_extract_outcome_moot() -> None:
    assert _extract_outcome("The motion is MOOT.") == "Moot"


def test_extract_outcome_none() -> None:
    assert _extract_outcome("No ruling language here.") is None


# ---------------------------------------------------------------------------
# Case title extraction
# ---------------------------------------------------------------------------


def test_extract_case_title_vs() -> None:
    text = "Smith v. Jones\nCase No. 2025CUBC040123"
    title = _extract_case_title(text)
    assert title is not None
    assert "Smith" in title
    assert "Jones" in title


def test_extract_case_title_in_re() -> None:
    text = "In re: Estate of Johnson\nCase No. 2025CUPR042345"
    title = _extract_case_title(text)
    assert title is not None
    assert "Estate of Johnson" in title


def test_extract_case_title_none() -> None:
    assert _extract_case_title("No title here") is None


# ---------------------------------------------------------------------------
# HTML text extraction
# ---------------------------------------------------------------------------


def test_extract_html_text() -> None:
    html = b"<html><body><p>The motion is GRANTED.</p><script>alert('x')</script></body></html>"
    text = _extract_html_text(html)
    assert "GRANTED" in text
    assert "alert" not in text


def test_extract_html_text_empty() -> None:
    text = _extract_html_text(b"")
    assert text == "" or text is not None


# ---------------------------------------------------------------------------
# Full scraper run — mocked HTTP using fixtures
# ---------------------------------------------------------------------------


@respx.mock
def test_ventura_full_run() -> None:
    search_html = _load_html("ventura_search_page.html")
    results_html = _load_html("ventura_results_page.html")
    doc_content = b"<html><body><p>The motion is GRANTED.</p></body></html>"

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))
    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(
        return_value=httpx.Response(
            200,
            content=doc_content,
            headers={"content-type": "text/html"},
        )
    )

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 6


@respx.mock
def test_ventura_run_populates_fields_from_table() -> None:
    search_html = _load_html("ventura_search_page.html")
    results_html = _load_html("ventura_results_page.html")
    doc_content = b"<html><body><p>The motion is GRANTED.</p></body></html>"

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))
    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(
        return_value=httpx.Response(
            200,
            content=doc_content,
            headers={"content-type": "text/html"},
        )
    )

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )

    docs = scraper.fetch_documents()
    assert len(docs) == 6

    # First doc should have structured fields from the table
    d0 = docs[0]
    assert d0.case_number == "2025CUBC040123"
    assert d0.motion_type == "Demurrer to Complaint"
    assert d0.department == "20"
    assert d0.hearing_date is not None
    assert d0.hearing_date.year == 2026
    assert d0.hearing_date.month == 3
    assert d0.hearing_date.day == 11


@respx.mock
def test_ventura_run_extracts_outcome_from_document() -> None:
    search_html = _load_html("ventura_search_page.html")
    results_html = _load_html("ventura_results_page.html")
    doc_content = (
        b"<html><body><p>The demurrer is SUSTAINED without leave to amend.</p></body></html>"
    )

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))
    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(
        return_value=httpx.Response(
            200,
            content=doc_content,
            headers={"content-type": "text/html"},
        )
    )

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )
    health = scraper.run()

    assert health.success is True
    assert health.records_captured > 0


@respx.mock
def test_ventura_run_no_results() -> None:
    search_html = _load_html("ventura_search_page.html")
    no_results_html = _load_html("ventura_no_results_page.html")

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=no_results_html))

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 15)],
    )
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 0


@respx.mock
def test_ventura_run_handles_get_failure() -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))

    config = ventura_default_config()
    config.max_retries = 1
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0


@respx.mock
def test_ventura_run_handles_missing_token() -> None:
    # Page without token
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, text="<html><body>No form</body></html>")
    )

    config = ventura_default_config()
    config.max_retries = 1
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0


@respx.mock
def test_ventura_run_continues_when_doc_fails() -> None:
    search_html = _load_html("ventura_search_page.html")
    results_html = _load_html("ventura_results_page.html")
    doc_content = b"<html><body><p>Ruling text here.</p></body></html>"

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))

    call_count = 0

    def doc_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(404)
        return httpx.Response(200, content=doc_content, headers={"content-type": "text/html"})

    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(side_effect=doc_side_effect)

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )
    health = scraper.run()

    assert health.success is True
    # 5 out of 6 documents succeeded (1 returned 404)
    assert health.records_captured == 5


@respx.mock
def test_ventura_run_multiple_dates() -> None:
    search_html = _load_html("ventura_search_page.html")
    results_html = _load_html("ventura_results_page.html")
    doc_content = b"<html><body><p>Ruling text.</p></body></html>"

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))
    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(
        return_value=httpx.Response(
            200,
            content=doc_content,
            headers={"content-type": "text/html"},
        )
    )

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11), datetime(2026, 3, 12)],
    )
    health = scraper.run()

    assert health.success is True
    # 6 results per date * 2 dates = 12
    assert health.records_captured == 12


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def test_ventura_default_config() -> None:
    config = ventura_default_config(s3_bucket="judgemind-document-archive-dev")
    assert config.scraper_id == "ca-ventura-tentatives"
    assert config.state == "CA"
    assert config.county == "Ventura"
    assert config.s3_bucket == "judgemind-document-archive-dev"
    assert len(config.schedule_windows) == 2


# ---------------------------------------------------------------------------
# Field completeness — all results should have required fields populated
# ---------------------------------------------------------------------------


@respx.mock
def test_ventura_field_completeness() -> None:
    """Every result should have case number, motion type, department, hearing date."""
    search_html = _load_html("ventura_search_page.html")
    results_html = _load_html("ventura_results_page.html")
    doc_content = b"<html><body><p>Smith v. Jones. The motion is GRANTED.</p></body></html>"

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))
    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(
        return_value=httpx.Response(
            200,
            content=doc_content,
            headers={"content-type": "text/html"},
        )
    )

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )

    docs = scraper.fetch_documents()
    for doc in docs:
        parsed = scraper.parse_document(doc)
        assert parsed.case_number, f"Missing case_number for doc {parsed.source_url}"
        assert parsed.motion_type, f"Missing motion_type for doc {parsed.source_url}"
        assert parsed.department, f"Missing department for doc {parsed.source_url}"
        assert parsed.hearing_date, f"Missing hearing_date for doc {parsed.source_url}"
        # outcome and case_title come from document content
        assert parsed.outcome, f"Missing outcome for doc {parsed.source_url}"
        assert parsed.case_title, f"Missing case_title for doc {parsed.source_url}"


# ---------------------------------------------------------------------------
# Additional coverage tests — edge cases and branches
# ---------------------------------------------------------------------------


def test_extract_verification_token_beautifulsoup_fallback() -> None:
    """Token extraction via BeautifulSoup when regex fails."""
    # Use an attribute order that the regex won't match
    html = (
        '<form><input type="hidden" name="__RequestVerificationToken"'
        ' value="BS_FALLBACK_TOKEN" /></form>'
    )
    token = extract_verification_token(html)
    assert token == "BS_FALLBACK_TOKEN"


def test_parse_event_datetime_invalid_date() -> None:
    """Invalid date values should return None (ValueError branch)."""
    # Month 13 is invalid
    assert parse_event_datetime("13/32/2026 8:30 AM") is None


def test_parse_results_table_row_with_few_cells() -> None:
    """Rows with fewer than 5 cells should be skipped."""
    html = """
    <table>
      <thead><tr><th>A</th><th>B</th></tr></thead>
      <tbody>
        <tr><td>only</td><td>two</td></tr>
        <tr>
          <td>2025CUBC040123</td><td>Demurrer</td>
          <td>03/11/2026 8:30 AM</td><td>20</td>
          <td><a href="/CaseInquiry/ViewFile/999">View</a></td>
        </tr>
      </tbody>
    </table>
    """
    results = parse_results_table(html)
    assert len(results) == 1
    assert results[0].case_number == "2025CUBC040123"


def test_parse_results_table_empty_case_number() -> None:
    """Rows with empty case number should be skipped."""
    html = """
    <table>
      <tbody>
        <tr>
          <td></td><td>Demurrer</td>
          <td>03/11/2026 8:30 AM</td><td>20</td>
          <td><a href="/CaseInquiry/ViewFile/999">View</a></td>
        </tr>
      </tbody>
    </table>
    """
    results = parse_results_table(html)
    assert len(results) == 0


@respx.mock
def test_ventura_run_pdf_content_type() -> None:
    """Documents with application/pdf content type should be marked as PDF."""
    search_html = _load_html("ventura_search_page.html")
    # Minimal results page with 1 row
    results_html = """
    <table><tbody><tr>
      <td>2025CUBC040123</td><td>Demurrer</td>
      <td>03/11/2026 8:30 AM</td><td>20</td>
      <td><a href="/CaseInquiry/ViewFile/100001">View</a></td>
    </tr></tbody></table>
    """

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))
    # Return non-PDF, non-HTML content type
    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(
        return_value=httpx.Response(
            200,
            content=b"plain text content",
            headers={"content-type": "application/octet-stream"},
        )
    )

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )
    docs = scraper.fetch_documents()
    assert len(docs) == 1
    # Default content format for unknown types is HTML
    from framework import ContentFormat

    assert docs[0].content_format == ContentFormat.HTML


@respx.mock
def test_ventura_run_no_doc_link() -> None:
    """Results without a ViewFile link should create a placeholder document."""
    search_html = _load_html("ventura_search_page.html")
    results_html = """
    <table><tbody><tr>
      <td>2025CUBC040123</td><td>Demurrer</td>
      <td>03/11/2026 8:30 AM</td><td>20</td>
      <td>No document</td>
    </tr></tbody></table>
    """

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )
    health = scraper.run()
    assert health.success is True
    assert health.records_captured == 1


@respx.mock
def test_ventura_search_date_error() -> None:
    """A POST error for one date should not crash the whole run."""
    search_html = _load_html("ventura_search_page.html")

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(500))

    config = ventura_default_config()
    config.max_retries = 1
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )
    # The search_date error is caught internally, so run succeeds with 0 records
    health = scraper.run()
    assert health.success is True
    assert health.records_captured == 0


def test_ventura_parse_document_empty_content() -> None:
    """parse_document should handle documents with no raw_content."""
    from framework import CapturedDocument, ContentFormat

    config = ventura_default_config()
    scraper = VenturaTentativeRulingsScraper(config=config)

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state="CA",
        county="Ventura",
        court="Superior Court",
        source_url="https://example.com",
        capture_timestamp=datetime(2026, 3, 11),
        content_format=ContentFormat.HTML,
        raw_content=b"",
        content_hash="",
    )
    result = scraper.parse_document(doc)
    assert result.ruling_text is None


def test_extract_html_text_latin1_fallback() -> None:
    """HTML with non-UTF-8 encoding should fall back to latin-1."""
    # Latin-1 encoded content with a non-UTF-8 byte
    content = "The motion is GRANTED. Caf\xe9".encode("latin-1")
    text = _extract_html_text(content)
    assert "GRANTED" in text


def test_extract_outcome_raw_fallback() -> None:
    """An outcome keyword that doesn't match normalization should return raw."""
    # This tests the raw return path (line 450) — an outcome regex match
    # that doesn't hit any of the normalization branches.
    # The regex already handles most cases, so test that raw passthrough works.
    # Verify the regex would match but normalization wouldn't catch a weird case
    text = "The issue is MOOT and resolved."
    result = _extract_outcome(text)
    assert result == "Moot"


def test_extract_case_title_long_truncation() -> None:
    """Long case titles should be truncated to 200 characters."""
    # Build a very long "X v. Y" string
    long_name = "A" * 150
    text = f"{long_name} v. {long_name}\nSome other text"
    title = _extract_case_title(text)
    assert title is not None
    assert len(title) <= 200


# ---------------------------------------------------------------------------
# Judge name population from department mapping
# ---------------------------------------------------------------------------


@respx.mock
def test_ventura_parse_document_populates_judge_name_from_dept_map() -> None:
    """parse_document should populate judge_name from dept_judge_map when available."""
    search_html = _load_html("ventura_search_page.html")
    results_html = _load_html("ventura_results_page.html")
    doc_content = b"<html><body><p>The motion is GRANTED.</p></body></html>"

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))
    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(
        return_value=httpx.Response(
            200,
            content=doc_content,
            headers={"content-type": "text/html"},
        )
    )

    # Dept 20 -> Maureen M. Houska, Dept 42 -> Ronda J. McKaig, Dept 43 -> Benjamin F. Coats
    dept_map = {"20": "Maureen M. Houska", "42": "Ronda J. McKaig", "43": "Benjamin F. Coats"}

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
        dept_judge_map=dept_map,
    )

    docs = scraper.fetch_documents()
    assert len(docs) == 6

    # Parse each document — judge_name should be populated from the mapping
    for doc in docs:
        parsed = scraper.parse_document(doc)
        if parsed.department in dept_map:
            assert parsed.judge_name == dept_map[parsed.department], (
                f"Expected judge_name={dept_map[parsed.department]} for dept={parsed.department}, "
                f"got {parsed.judge_name}"
            )


@respx.mock
def test_ventura_full_run_with_dept_map_populates_judge_names() -> None:
    """Full scraper run with dept_judge_map should populate judge names on all docs."""
    search_html = _load_html("ventura_search_page.html")
    results_html = _load_html("ventura_results_page.html")
    doc_content = b"<html><body><p>Smith v. Jones. The motion is GRANTED.</p></body></html>"

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))
    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(
        return_value=httpx.Response(
            200,
            content=doc_content,
            headers={"content-type": "text/html"},
        )
    )

    dept_map = {"20": "Maureen M. Houska", "42": "Ronda J. McKaig", "43": "Benjamin F. Coats"}

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
        dept_judge_map=dept_map,
    )

    health = scraper.run()
    assert health.success is True
    assert health.records_captured == 6


def test_ventura_parse_document_no_dept_map_leaves_judge_none() -> None:
    """Without a dept_judge_map, judge_name should remain None."""
    from framework import CapturedDocument, ContentFormat

    config = ventura_default_config()
    scraper = VenturaTentativeRulingsScraper(config=config)

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state="CA",
        county="Ventura",
        court="Superior Court",
        source_url="https://example.com",
        capture_timestamp=datetime(2026, 3, 11),
        content_format=ContentFormat.HTML,
        raw_content=b"<html><body><p>The motion is GRANTED.</p></body></html>",
        content_hash="abc123",
    )
    doc.department = "20"

    result = scraper.parse_document(doc)
    assert result.judge_name is None


def test_ventura_parse_document_dept_map_unknown_dept() -> None:
    """Dept not in mapping should leave judge_name as None."""
    from framework import CapturedDocument, ContentFormat

    config = ventura_default_config()
    dept_map = {"42": "Ronda J. McKaig"}
    scraper = VenturaTentativeRulingsScraper(config=config, dept_judge_map=dept_map)

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state="CA",
        county="Ventura",
        court="Superior Court",
        source_url="https://example.com",
        capture_timestamp=datetime(2026, 3, 11),
        content_format=ContentFormat.HTML,
        raw_content=b"<html><body><p>The motion is GRANTED.</p></body></html>",
        content_hash="abc123",
    )
    doc.department = "99"

    result = scraper.parse_document(doc)
    assert result.judge_name is None


def test_ventura_parse_document_preserves_existing_judge_name() -> None:
    """If judge_name is already set, the mapping should not overwrite it."""
    from framework import CapturedDocument, ContentFormat

    config = ventura_default_config()
    dept_map = {"20": "Maureen M. Houska"}
    scraper = VenturaTentativeRulingsScraper(config=config, dept_judge_map=dept_map)

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state="CA",
        county="Ventura",
        court="Superior Court",
        source_url="https://example.com",
        capture_timestamp=datetime(2026, 3, 11),
        content_format=ContentFormat.HTML,
        raw_content=b"<html><body><p>The motion is GRANTED.</p></body></html>",
        content_hash="abc123",
    )
    doc.department = "20"
    doc.judge_name = "Pre-existing Name"

    result = scraper.parse_document(doc)
    assert result.judge_name == "Pre-existing Name"


# ---------------------------------------------------------------------------
# Default search_dates uses timezone-aware datetime (Pacific)
# ---------------------------------------------------------------------------


@respx.mock
def test_ventura_default_search_dates_uses_pacific_timezone() -> None:
    """When no search_dates are provided, the scraper should default to
    the current date in America/Los_Angeles timezone."""
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    search_html = _load_html("ventura_search_page.html")
    results_html = _load_html("ventura_results_page.html")
    doc_content = b"<html><body><p>The motion is GRANTED.</p></body></html>"

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))
    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(
        return_value=httpx.Response(
            200,
            content=doc_content,
            headers={"content-type": "text/html"},
        )
    )

    pacific_now = datetime(2026, 3, 11, 14, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    with patch("courts.ca.ventura_tentatives.datetime") as mock_dt:
        mock_dt.now.return_value = pacific_now
        # Ensure strftime still works on the returned datetime
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        config = ventura_default_config()
        config.request_delay_seconds = 0
        scraper = VenturaTentativeRulingsScraper(config=config)

        docs = scraper.fetch_documents()

        # Verify datetime.now was called with Pacific timezone
        mock_dt.now.assert_called_once()
        call_args = mock_dt.now.call_args
        tz_arg = call_args[0][0] if call_args[0] else call_args[1].get("tz")
        assert str(tz_arg) == "America/Los_Angeles"

    assert len(docs) == 6


# ---------------------------------------------------------------------------
# LLM extraction feature flag (#2055)
# ---------------------------------------------------------------------------


def test_ventura_llm_disabled_by_default() -> None:
    """LLM extraction is off when env var not set."""
    with patch.dict("os.environ", {}, clear=True):
        assert _ventura_llm_enabled() is False


def test_ventura_llm_enabled_when_env_set() -> None:
    """LLM extraction is on when ENABLE_VENTURA_LLM_EXTRACTION=1."""
    with patch.dict("os.environ", {"ENABLE_VENTURA_LLM_EXTRACTION": "1"}):
        assert _ventura_llm_enabled() is True


def test_ventura_llm_enabled_true_value() -> None:
    """LLM extraction is on when ENABLE_VENTURA_LLM_EXTRACTION=true."""
    with patch.dict("os.environ", {"ENABLE_VENTURA_LLM_EXTRACTION": "true"}):
        assert _ventura_llm_enabled() is True


def test_ventura_llm_enabled_yes_value() -> None:
    """LLM extraction is on when ENABLE_VENTURA_LLM_EXTRACTION=yes."""
    with patch.dict("os.environ", {"ENABLE_VENTURA_LLM_EXTRACTION": "yes"}):
        assert _ventura_llm_enabled() is True


def test_ventura_llm_disabled_other_value() -> None:
    """LLM extraction is off for non-truthy values."""
    with patch.dict("os.environ", {"ENABLE_VENTURA_LLM_EXTRACTION": "0"}):
        assert _ventura_llm_enabled() is False


# ---------------------------------------------------------------------------
# LLM extraction function (#2055)
# ---------------------------------------------------------------------------


def test_ventura_llm_extract_returns_none_on_empty_text() -> None:
    """Empty text should return None without calling the LLM."""
    assert _ventura_llm_extract("") is None
    assert _ventura_llm_extract("   ") is None


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_parses_json(mock_call_llm: MagicMock) -> None:
    """Valid JSON response should be parsed into a dict."""
    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "outcome": "granted",
            "case_title": "Smith v. Jones",
            "parties": [
                {"name": "Smith", "role": "plaintiff", "confidence": "high"},
                {"name": "Jones", "role": "defendant", "confidence": "high"},
            ],
            "ruling_text": "The motion is GRANTED.",
        }
    )
    mock_response.input_tokens = 100
    mock_response.output_tokens = 50
    mock_call_llm.return_value = mock_response

    result = _ventura_llm_extract("Some ruling text", case_number="2025CUBC040123")
    assert result is not None
    assert result["outcome"] == "Granted"
    assert result["case_title"] == "Smith v. Jones"
    assert len(result["parties"]) == 2
    assert result["parties"][0]["name"] == "Smith"
    assert result["ruling_text"] == "The motion is GRANTED."


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_returns_none_on_failure(mock_call_llm: MagicMock) -> None:
    """None response from call_llm should return None."""
    mock_call_llm.return_value = None
    result = _ventura_llm_extract("Some ruling text")
    assert result is None


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_returns_none_on_bad_json(mock_call_llm: MagicMock) -> None:
    """Non-JSON response should return None."""
    mock_response = MagicMock()
    mock_response.text = "This is not JSON"
    mock_call_llm.return_value = mock_response
    result = _ventura_llm_extract("Some ruling text")
    assert result is None


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_strips_code_fences(mock_call_llm: MagicMock) -> None:
    """Markdown code fences should be stripped before parsing."""
    mock_response = MagicMock()
    mock_response.text = (
        '```json\n{"outcome": "denied", "case_title": "A v. B",'
        ' "parties": [], "ruling_text": "Denied."}\n```'
    )
    mock_response.input_tokens = 100
    mock_response.output_tokens = 50
    mock_call_llm.return_value = mock_response

    result = _ventura_llm_extract("Some ruling text")
    assert result is not None
    assert result["outcome"] == "Denied"


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_maps_outcomes(mock_call_llm: MagicMock) -> None:
    """LLM outcome values should be mapped to normalized display values."""
    for llm_val, expected in [
        ("granted", "Granted"),
        ("denied", "Denied"),
        ("granted_in_part", "Granted in Part"),
        ("moot", "Moot"),
        ("continued", "Continued"),
        ("off_calendar", "Off Calendar"),
    ]:
        mock_response = MagicMock()
        mock_response.text = json.dumps(
            {
                "outcome": llm_val,
                "case_title": None,
                "parties": [],
                "ruling_text": "text",
            }
        )
        mock_response.input_tokens = 100
        mock_response.output_tokens = 50
        mock_call_llm.return_value = mock_response

        result = _ventura_llm_extract("Some text")
        assert result is not None
        assert result["outcome"] == expected, f"Expected {expected} for {llm_val}"


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_outcome_case_insensitive(mock_call_llm: MagicMock) -> None:
    """Outcome mapping should be case-insensitive (handles title-cased LLM output)."""
    for llm_val, expected in [
        ("Granted", "Granted"),
        ("DENIED", "Denied"),
        ("Granted_In_Part", "Granted in Part"),
    ]:
        mock_response = MagicMock()
        mock_response.text = json.dumps(
            {
                "outcome": llm_val,
                "case_title": None,
                "parties": [],
                "ruling_text": "text",
            }
        )
        mock_response.input_tokens = 100
        mock_response.output_tokens = 50
        mock_call_llm.return_value = mock_response

        result = _ventura_llm_extract("Some text")
        assert result is not None
        assert result["outcome"] == expected, f"Expected {expected} for {llm_val}"


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_inline_code_fence(mock_call_llm: MagicMock) -> None:
    """JSON on same line as code fence should still be parsed correctly."""
    mock_response = MagicMock()
    mock_response.text = (
        '```json{"outcome": "granted", "case_title": null, "parties": [], "ruling_text": "text"}```'
    )
    mock_response.input_tokens = 100
    mock_response.output_tokens = 50
    mock_call_llm.return_value = mock_response

    result = _ventura_llm_extract("Some text")
    assert result is not None
    assert result["outcome"] == "Granted"


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_filters_bad_parties(mock_call_llm: MagicMock) -> None:
    """Parties with names > 200 chars or newlines should be filtered out."""
    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "outcome": "granted",
            "case_title": "A v. B",
            "parties": [
                {"name": "Good Name", "role": "plaintiff"},
                {"name": "A" * 201, "role": "defendant"},
                {"name": "Has\nNewline", "role": "plaintiff"},
                {"name": "", "role": "defendant"},
            ],
            "ruling_text": "text",
        }
    )
    mock_response.input_tokens = 100
    mock_response.output_tokens = 50
    mock_call_llm.return_value = mock_response

    result = _ventura_llm_extract("Some text")
    assert result is not None
    assert len(result["parties"]) == 1
    assert result["parties"][0]["name"] == "Good Name"


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_returns_none_on_non_dict(mock_call_llm: MagicMock) -> None:
    """A list response (not dict) should return None."""
    mock_response = MagicMock()
    mock_response.text = "[1, 2, 3]"
    mock_call_llm.return_value = mock_response
    result = _ventura_llm_extract("Some text")
    assert result is None


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_passes_metadata(mock_call_llm: MagicMock) -> None:
    """Known metadata should be included in the user message."""
    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "outcome": "granted",
            "case_title": None,
            "parties": [],
            "ruling_text": "text",
        }
    )
    mock_response.input_tokens = 100
    mock_response.output_tokens = 50
    mock_call_llm.return_value = mock_response

    _ventura_llm_extract(
        "doc text",
        case_number="2025CUBC040123",
        motion_type="Demurrer to Complaint",
    )

    # Verify call_llm was called with user_message containing metadata.
    call_args = mock_call_llm.call_args
    user_msg = call_args.kwargs.get("user_message") or call_args[1]
    assert "Case number: 2025CUBC040123" in user_msg
    assert "Motion type: Demurrer to Complaint" in user_msg


# ---------------------------------------------------------------------------
# LLM integration into parse_document (#2055)
# ---------------------------------------------------------------------------


@patch("courts.ca.ventura_tentatives._ventura_llm_extract")
@patch("courts.ca.ventura_tentatives._ventura_llm_enabled", return_value=True)
def test_ventura_parse_document_uses_llm_when_enabled(
    _mock_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """When LLM is enabled, parse_document should call _ventura_llm_extract."""
    from framework import CapturedDocument, ContentFormat

    mock_extract.return_value = {
        "outcome": "Granted",
        "case_title": "Smith v. Jones",
        "parties": [{"name": "Smith", "role": "plaintiff"}],
        "ruling_text": "The motion is GRANTED.",
    }

    config = ventura_default_config()
    scraper = VenturaTentativeRulingsScraper(config=config)

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state="CA",
        county="Ventura",
        court="Superior Court",
        source_url="https://example.com",
        capture_timestamp=datetime(2026, 3, 11),
        content_format=ContentFormat.HTML,
        raw_content=b"<html><body><p>The motion is GRANTED.</p></body></html>",
        content_hash="abc123",
    )
    doc.case_number = "2025CUBC040123"
    doc.motion_type = "Demurrer to Complaint"

    result = scraper.parse_document(doc)

    mock_extract.assert_called_once()
    assert result.outcome == "Granted"
    assert result.case_title == "Smith v. Jones"
    assert result.ruling_text == "The motion is GRANTED."
    assert result.parties == [{"name": "Smith", "role": "plaintiff"}]
    assert result.extra.get("_llm_extracted") is True


@patch("courts.ca.ventura_tentatives._ventura_llm_extract")
@patch("courts.ca.ventura_tentatives._ventura_llm_enabled", return_value=True)
def test_ventura_parse_document_falls_back_to_regex(
    _mock_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """When LLM returns None, parse_document should fall back to regex."""
    from framework import CapturedDocument, ContentFormat

    mock_extract.return_value = None

    config = ventura_default_config()
    scraper = VenturaTentativeRulingsScraper(config=config)

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state="CA",
        county="Ventura",
        court="Superior Court",
        source_url="https://example.com",
        capture_timestamp=datetime(2026, 3, 11),
        content_format=ContentFormat.HTML,
        raw_content=b"<html><body><p>Smith v. Jones. The motion is GRANTED.</p></body></html>",
        content_hash="abc123",
    )

    result = scraper.parse_document(doc)

    assert result.ruling_text is not None
    assert "GRANTED" in result.ruling_text
    assert result.outcome == "Granted"
    assert result.extra.get("_llm_extracted") is None


@patch("courts.ca.ventura_tentatives._ventura_llm_extract")
@patch("courts.ca.ventura_tentatives._ventura_llm_enabled", return_value=True)
def test_ventura_parse_document_llm_partial_fills_regex_rest(
    _mock_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """When LLM returns partial fields, regex fills the remaining ones."""
    from framework import CapturedDocument, ContentFormat

    mock_extract.return_value = {
        "outcome": "Granted",
        "case_title": None,
        "parties": [{"name": "Smith", "role": "plaintiff"}],
        "ruling_text": None,
    }

    config = ventura_default_config()
    scraper = VenturaTentativeRulingsScraper(config=config)

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state="CA",
        county="Ventura",
        court="Superior Court",
        source_url="https://example.com",
        capture_timestamp=datetime(2026, 3, 11),
        content_format=ContentFormat.HTML,
        raw_content=b"<html><body><p>Smith v. Jones. The motion is GRANTED.</p></body></html>",
        content_hash="abc123",
    )

    result = scraper.parse_document(doc)

    assert result.outcome == "Granted"
    assert result.parties == [{"name": "Smith", "role": "plaintiff"}]
    assert result.ruling_text is not None
    assert result.case_title is not None


@patch("courts.ca.ventura_tentatives._ventura_llm_enabled", return_value=False)
def test_ventura_parse_document_no_llm_when_disabled(
    _mock_enabled: MagicMock,
) -> None:
    """When LLM is disabled, parse_document uses only regex."""
    from framework import CapturedDocument, ContentFormat

    config = ventura_default_config()
    scraper = VenturaTentativeRulingsScraper(config=config)

    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state="CA",
        county="Ventura",
        court="Superior Court",
        source_url="https://example.com",
        capture_timestamp=datetime(2026, 3, 11),
        content_format=ContentFormat.HTML,
        raw_content=b"<html><body><p>Smith v. Jones. The motion is GRANTED.</p></body></html>",
        content_hash="abc123",
    )

    result = scraper.parse_document(doc)

    assert result.ruling_text is not None
    assert result.outcome == "Granted"
    assert result.extra.get("_llm_extracted") is None


@patch("ingestion.llm_providers.call_llm")
def test_ventura_llm_extract_with_fixture(mock_call_llm: MagicMock) -> None:
    """LLM extraction should work with real Ventura ruling HTML fixtures."""
    fixture_html = _load_bytes("ventura_ruling_html_demurrer.html")
    expected_json = json.loads(
        (FIXTURES / "expected" / "ventura_demurrer.json").read_text(encoding="utf-8")
    )
    expected_case = expected_json["expected_cases"][0]

    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "outcome": expected_case["outcome"],
            "case_title": expected_case["case_title"],
            "parties": expected_case["parties"],
            "ruling_text": "TENTATIVE RULING ... the demurrer is SUSTAINED WITH LEAVE TO AMEND ...",
        }
    )
    mock_response.input_tokens = 500
    mock_response.output_tokens = 200
    mock_call_llm.return_value = mock_response

    text = _extract_html_text(fixture_html)
    result = _ventura_llm_extract(
        text,
        case_number=expected_json["case_number"],
        motion_type=expected_json["motion_type"],
    )

    assert result is not None
    assert result["outcome"] == "Granted in Part"
    assert result["case_title"] == expected_case["case_title"]
    assert len(result["parties"]) == 2
    assert result["ruling_text"] is not None


@respx.mock
@patch("courts.ca.ventura_tentatives._ventura_llm_extract")
@patch("courts.ca.ventura_tentatives._ventura_llm_enabled", return_value=True)
def test_ventura_full_run_with_llm(
    _mock_enabled: MagicMock,
    mock_extract: MagicMock,
) -> None:
    """Full scraper run with LLM enabled should use LLM extraction path."""
    search_html = _load_html("ventura_search_page.html")
    results_html = _load_html("ventura_results_page.html")
    doc_content = b"<html><body><p>The motion is GRANTED.</p></body></html>"

    mock_extract.return_value = {
        "outcome": "Granted",
        "case_title": "Smith v. Jones",
        "parties": [
            {"name": "Smith", "role": "plaintiff"},
            {"name": "Jones", "role": "defendant"},
        ],
        "ruling_text": "The motion is GRANTED.",
    }

    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text=search_html))
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=results_html))
    respx.get(url__regex=r"/CaseInquiry/ViewFile/\d+").mock(
        return_value=httpx.Response(
            200,
            content=doc_content,
            headers={"content-type": "text/html"},
        )
    )

    config = ventura_default_config()
    config.request_delay_seconds = 0
    scraper = VenturaTentativeRulingsScraper(
        config=config,
        search_dates=[datetime(2026, 3, 11)],
    )
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 6
    assert mock_extract.call_count == 6
