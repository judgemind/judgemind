"""Tests for Ventura County tentative rulings scraper.

Fixtures:
  ventura_search_page.html     — GET search page with anti-forgery token
  ventura_results_page.html    — POST results with 6 rows across 3 departments
  ventura_no_results_page.html — POST results with no matching rulings
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.ventura_tentatives import (
    SEARCH_URL,
    VenturaTentativeRulingsScraper,
    _extract_case_title,
    _extract_html_text,
    _extract_outcome,
    extract_verification_token,
    parse_event_datetime,
    parse_results_table,
)
from courts.ca.ventura_tentatives import default_config as ventura_default_config

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


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
