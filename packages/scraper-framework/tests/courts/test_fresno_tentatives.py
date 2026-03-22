"""Tests for Fresno County tentative rulings scraper.

Fixtures captured from live site 2026-03-11:
  fresno_index_page.html               — GET https://www.fresno.courts.ca.gov/online-services/tentative-rulings
                                          (20 PDF links across 4 departments)
  fresno_403_20260310_d019042f.pdf     — Dept 403, 7 rulings, initials "lmg"
  fresno_501_20260310_10fe3c7f.pdf     — Dept 501, 2 rulings, initials "DTT"
  fresno_502_20260311_eef66c41.pdf     — Dept 502, 4 rulings, initials "KCK"
  fresno_503_20260311_03c051b7.pdf     — Dept 503, 2 rulings, initials various
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.fresno_tentatives import (
    _ISSUED_BY_RE,
    _NO_RULINGS_RE,
    BASE_URL,
    INDEX_URL,
    FresnoTentativeRulingsScraper,
    SplitRuling,
    _extract_case_number,
    _extract_case_title,
    _extract_motion_type,
    _extract_outcome,
    _fresno_courthouse,
    _fresno_dept_from_filename,
    _fresno_hearing_date_from_filename,
    _fresno_hearing_date_from_text,
    _normalize_outcome,
    _split_rulings,
)
from courts.ca.fresno_tentatives import default_config as fresno_default_config
from courts.ca.pdf_link_scraper import _extract_pdf_links, _extract_pdf_text

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# _extract_pdf_links — against real Fresno index page
# ---------------------------------------------------------------------------


def test_fresno_extract_pdf_links_count() -> None:
    html = _load_html("fresno_index_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    assert len(links) == 20


def test_fresno_extract_pdf_links_absolute_urls() -> None:
    html = _load_html("fresno_index_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    for url, _ in links:
        assert url.startswith("http"), f"Expected absolute URL, got {url!r}"
        assert ".pdf" in url.lower()


def test_fresno_extract_pdf_links_no_duplicates() -> None:
    html = _load_html("fresno_index_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    urls = [u for u, _ in links]
    assert len(urls) == len(set(urls))


def test_fresno_extract_pdf_links_first_entry() -> None:
    html = _load_html("fresno_index_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    url, text = links[0]
    assert "dept-403" in url
    assert "March" in text


# ---------------------------------------------------------------------------
# _extract_pdf_text — against real Fresno fixture PDFs
# ---------------------------------------------------------------------------


def test_fresno_403_pdf_text_extraction() -> None:
    text = _extract_pdf_text(_load_bytes("fresno_403_20260310_d019042f.pdf"))
    assert "Department 403" in text
    assert "Lopez" in text


def test_fresno_501_pdf_text_extraction() -> None:
    text = _extract_pdf_text(_load_bytes("fresno_501_20260310_10fe3c7f.pdf"))
    assert "Department 501" in text
    assert "Godines" in text


def test_fresno_502_pdf_text_extraction() -> None:
    text = _extract_pdf_text(_load_bytes("fresno_502_20260311_eef66c41.pdf"))
    assert "Department 502" in text


def test_fresno_503_pdf_text_extraction() -> None:
    text = _extract_pdf_text(_load_bytes("fresno_503_20260311_03c051b7.pdf"))
    assert "Department 503" in text
    assert "Vartanian" in text


# ---------------------------------------------------------------------------
# Department extraction from filename
# ---------------------------------------------------------------------------


def test_fresno_dept_from_filename_403() -> None:
    assert _fresno_dept_from_filename("03-10-26-dept-403.pdf") == "403"


def test_fresno_dept_from_filename_501() -> None:
    assert _fresno_dept_from_filename("03-10-26-dept-501.pdf") == "501"


def test_fresno_dept_from_filename_with_suffix() -> None:
    assert _fresno_dept_from_filename("03-04-26-dept-403_0.pdf") == "403"


def test_fresno_dept_from_filename_none_for_bad() -> None:
    assert _fresno_dept_from_filename("random.pdf") is None
    assert _fresno_dept_from_filename("") is None


# ---------------------------------------------------------------------------
# Hearing date from filename
# ---------------------------------------------------------------------------


def test_fresno_hearing_date_from_filename_403() -> None:
    dt = _fresno_hearing_date_from_filename("03-10-26-dept-403.pdf")
    assert dt == datetime(2026, 3, 10)


def test_fresno_hearing_date_from_filename_502() -> None:
    dt = _fresno_hearing_date_from_filename("03-11-26-dept-502.pdf")
    assert dt == datetime(2026, 3, 11)


def test_fresno_hearing_date_from_filename_with_suffix() -> None:
    dt = _fresno_hearing_date_from_filename("03-04-26-dept-403_0.pdf")
    assert dt == datetime(2026, 3, 4)


def test_fresno_hearing_date_from_filename_none_for_bad() -> None:
    assert _fresno_hearing_date_from_filename("random.pdf") is None
    assert _fresno_hearing_date_from_filename("") is None


# ---------------------------------------------------------------------------
# Hearing date from PDF text
# ---------------------------------------------------------------------------


def test_fresno_hearing_date_from_text() -> None:
    text = "Tentative Rulings for March 10, 2026\nDepartment 403"
    dt = _fresno_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 10)


def test_fresno_hearing_date_from_ruling_header() -> None:
    text = "Hearing Date: March 11, 2026 (Dept. 502)"
    dt = _fresno_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 11)


# ---------------------------------------------------------------------------
# Case title extraction
# ---------------------------------------------------------------------------


def test_fresno_extract_case_title() -> None:
    text = "Re: Lopez v. Fresno Unified School District\nSuperior Court Case No. 25CECG03271"
    assert _extract_case_title(text) == "Lopez v. Fresno Unified School District"


def test_fresno_extract_case_title_in_re() -> None:
    text = "Re: In re: Graceson A. Vongsaly\nSuperior Court Case No. 26CECG00722"
    assert _extract_case_title(text) == "In re: Graceson A. Vongsaly"


def test_fresno_extract_case_title_none() -> None:
    assert _extract_case_title("No Re: line here") is None


# ---------------------------------------------------------------------------
# Case number extraction
# ---------------------------------------------------------------------------


def test_fresno_extract_case_number_structured() -> None:
    text = "Superior Court Case No. 25CECG03271\nHearing Date: ..."
    assert _extract_case_number(text) == "25CECG03271"


def test_fresno_extract_case_number_short_form() -> None:
    text = "Case No. 23CECG05097\nHearing Date: ..."
    assert _extract_case_number(text) == "23CECG05097"


def test_fresno_extract_case_number_court_case() -> None:
    text = "Court Case No. 23CECG03612\nHearing Date: ..."
    assert _extract_case_number(text) == "23CECG03612"


def test_fresno_extract_case_number_none() -> None:
    assert _extract_case_number("No case number here") is None


# ---------------------------------------------------------------------------
# Motion type extraction
# ---------------------------------------------------------------------------


def test_fresno_extract_motion_demurrer() -> None:
    text = "Motion: Demurrer to First Amended Complaint\nTentative Ruling:"
    assert _extract_motion_type(text) == "Demurrer to First Amended Complaint"


def test_fresno_extract_motion_default_proveup() -> None:
    text = "Motion: Default Prove-Up\nTentative Ruling:"
    assert _extract_motion_type(text) == "Default Prove-Up"


def test_fresno_extract_motion_multiline() -> None:
    text = (
        "Motion: by Plaintiff to Compel Further Responses to Form\n"
        "Interrogatories, Special Interrogatories, Requests for\n"
        "Production of Documents, and Requests for Admissions, Set\n"
        "One, and for Sanctions\n"
        "Tentative Ruling:"
    )
    result = _extract_motion_type(text)
    assert result is not None
    assert "Compel Further Responses" in result


def test_fresno_extract_motion_none() -> None:
    assert _extract_motion_type("No motion line here") is None


# ---------------------------------------------------------------------------
# Outcome extraction
# ---------------------------------------------------------------------------


def test_fresno_outcome_to_grant() -> None:
    text = "Tentative Ruling:\nTo Grant.\nExplanation:"
    assert _extract_outcome(text) == "Granted"


def test_fresno_outcome_to_deny() -> None:
    text = "Tentative Ruling:\nTo deny without prejudice.\nExplanation:"
    assert _extract_outcome(text) == "Denied without Prejudice"


def test_fresno_outcome_to_sustain_without_leave() -> None:
    text = "Tentative Ruling:\nTo sustain the demurrers without leave to amend."
    assert _extract_outcome(text) == "Sustained without Leave to Amend"


def test_fresno_outcome_to_deny_petition() -> None:
    text = "Tentative Ruling:\nTo deny the petition.\nExplanation:"
    assert _extract_outcome(text) == "Denied"


def test_fresno_outcome_grant_in_part() -> None:
    text = "Tentative Ruling:\nTo grant, in part, and deny in part."
    assert _extract_outcome(text) == "Granted in Part"


def test_fresno_outcome_off_calendar() -> None:
    text = "Tentative Ruling:\nTo take the matter off calendar as moot."
    assert _extract_outcome(text) == "Off Calendar"


def test_fresno_outcome_off_calendar_order() -> None:
    text = "Tentative Ruling:\nTo order the motion off calendar."
    assert _extract_outcome(text) == "Off Calendar"


def test_fresno_outcome_continued() -> None:
    text = "Tentative Ruling:\nTo continue the motion to July 9, 2026."
    assert _extract_outcome(text) == "Continued"


def test_fresno_outcome_none() -> None:
    assert _extract_outcome("No outcome here") is None


# ---------------------------------------------------------------------------
# Ruling splitting — against real fixture PDFs
# ---------------------------------------------------------------------------


def test_fresno_split_rulings_403() -> None:
    text = _extract_pdf_text(_load_bytes("fresno_403_20260310_d019042f.pdf"))
    rulings = _split_rulings(text)
    # Dept 403 PDF has 8 rulings
    assert len(rulings) == 8
    # First ruling should have case number and title
    r0 = rulings[0]
    assert r0.case_number is not None
    assert "CECG" in r0.case_number
    assert r0.case_title is not None
    assert r0.motion_type is not None
    assert r0.outcome is not None


def test_fresno_split_rulings_501() -> None:
    text = _extract_pdf_text(_load_bytes("fresno_501_20260310_10fe3c7f.pdf"))
    rulings = _split_rulings(text)
    assert len(rulings) >= 2
    r0 = rulings[0]
    assert r0.case_number is not None
    assert r0.case_title is not None


def test_fresno_split_rulings_502() -> None:
    text = _extract_pdf_text(_load_bytes("fresno_502_20260311_eef66c41.pdf"))
    rulings = _split_rulings(text)
    assert len(rulings) >= 3
    for r in rulings:
        assert r.case_title is not None
        assert r.outcome is not None


def test_fresno_split_rulings_503() -> None:
    text = _extract_pdf_text(_load_bytes("fresno_503_20260311_03c051b7.pdf"))
    rulings = _split_rulings(text)
    assert len(rulings) >= 1
    r0 = rulings[0]
    assert r0.case_number is not None
    assert r0.case_title is not None


# ---------------------------------------------------------------------------
# Field completeness — against real fixture PDFs
# ---------------------------------------------------------------------------


def test_fresno_403_field_completeness() -> None:
    """Every ruling in Dept 403 must have case number, title, motion, and outcome."""
    text = _extract_pdf_text(_load_bytes("fresno_403_20260310_d019042f.pdf"))
    rulings = _split_rulings(text)
    assert len(rulings) == 8
    for r in rulings:
        assert r.case_number is not None, f"Missing case_number in ruling {r.ruling_index}"
        assert r.case_title is not None, f"Missing case_title in ruling {r.ruling_index}"
        assert r.motion_type is not None, f"Missing motion_type in ruling {r.ruling_index}"
        assert r.outcome is not None, f"Missing outcome in ruling {r.ruling_index}"
        assert r.hearing_date is not None, f"Missing hearing_date in ruling {r.ruling_index}"


def test_fresno_501_field_completeness() -> None:
    """Every ruling in Dept 501 must have case number, title, motion, and outcome."""
    text = _extract_pdf_text(_load_bytes("fresno_501_20260310_10fe3c7f.pdf"))
    rulings = _split_rulings(text)
    for r in rulings:
        assert r.case_number is not None, f"Missing case_number in ruling {r.ruling_index}"
        assert r.case_title is not None, f"Missing case_title in ruling {r.ruling_index}"
        assert r.motion_type is not None, f"Missing motion_type in ruling {r.ruling_index}"
        assert r.outcome is not None, f"Missing outcome in ruling {r.ruling_index}"


def test_fresno_502_field_completeness() -> None:
    """Every ruling in Dept 502 must have case number, title, motion, and outcome."""
    text = _extract_pdf_text(_load_bytes("fresno_502_20260311_eef66c41.pdf"))
    rulings = _split_rulings(text)
    for r in rulings:
        assert r.case_number is not None, f"Missing case_number in ruling {r.ruling_index}"
        assert r.case_title is not None, f"Missing case_title in ruling {r.ruling_index}"
        assert r.motion_type is not None, f"Missing motion_type in ruling {r.ruling_index}"
        assert r.outcome is not None, f"Missing outcome in ruling {r.ruling_index}"


# ---------------------------------------------------------------------------
# Full scraper run — mocked HTTP using real fixtures
# ---------------------------------------------------------------------------


@respx.mock
def test_fresno_full_run() -> None:
    html = _load_html("fresno_index_page.html")
    pdf_bytes = _load_bytes("fresno_403_20260310_d019042f.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = fresno_default_config()
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    # 20 PDFs, each with multiple rulings from the 403 fixture
    assert health.records_captured > 0


@respx.mock
def test_fresno_run_populates_dept_from_filename() -> None:
    html = _load_html("fresno_index_page.html")
    pdf_bytes = _load_bytes("fresno_403_20260310_d019042f.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = fresno_default_config()
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    assert len(docs) > 0

    # First doc should have department extracted from filename
    first = docs[0]
    assert first.department is not None
    assert first.department in ("403", "501", "502", "503")


@respx.mock
def test_fresno_run_splits_multi_ruling_pdfs() -> None:
    html = _load_html("fresno_index_page.html")
    pdf_bytes = _load_bytes("fresno_403_20260310_d019042f.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = fresno_default_config()
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    # Should be more docs than PDFs (20) due to splitting
    assert len(docs) > 20

    # Each doc should have case number and case title
    has_case = [d for d in docs if d.case_number]
    assert len(has_case) > 0

    has_title = [d for d in docs if d.case_title]
    assert len(has_title) > 0


@respx.mock
def test_fresno_run_extracts_hearing_date() -> None:
    html = _load_html("fresno_index_page.html")
    pdf_bytes = _load_bytes("fresno_403_20260310_d019042f.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = fresno_default_config()
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    has_date = [d for d in docs if d.hearing_date]
    assert len(has_date) > 0
    assert has_date[0].hearing_date == datetime(2026, 3, 10)


@respx.mock
def test_fresno_run_handles_get_failure() -> None:
    respx.get(INDEX_URL).mock(return_value=httpx.Response(503))

    config = fresno_default_config()
    config.max_retries = 1
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0


@respx.mock
def test_fresno_run_continues_when_pdf_fails() -> None:
    html = _load_html("fresno_index_page.html")
    pdf_bytes = _load_bytes("fresno_403_20260310_d019042f.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))

    call_count = 0

    def pdf_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(404)
        return httpx.Response(200, content=pdf_bytes)

    respx.get(url__regex=r"\.pdf").mock(side_effect=pdf_side_effect)

    config = fresno_default_config()
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    # 19 PDFs succeeded (1 failed), each split into multiple rulings
    assert health.records_captured > 0


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def test_fresno_default_config() -> None:
    config = fresno_default_config(s3_bucket="judgemind-document-archive-dev")
    assert config.scraper_id == "ca-fresno-tentatives-civil"
    assert config.state == "CA"
    assert config.county == "Fresno"
    assert config.s3_bucket == "judgemind-document-archive-dev"
    assert len(config.schedule_windows) == 2


# ---------------------------------------------------------------------------
# Specific ruling field tests against real PDFs
# ---------------------------------------------------------------------------


def test_fresno_403_ruling_lopez() -> None:
    """Verify specific fields from the Lopez ruling in Dept 403."""
    text = _extract_pdf_text(_load_bytes("fresno_403_20260310_d019042f.pdf"))
    rulings = _split_rulings(text)
    # Find the Lopez ruling
    lopez = [r for r in rulings if r.case_title and "Lopez" in r.case_title]
    assert len(lopez) >= 1
    r = lopez[0]
    assert r.case_number == "25CECG03271"
    assert r.case_title == "Lopez v. Fresno Unified School District"
    assert r.motion_type is not None
    assert "Demurrer" in r.motion_type
    assert r.outcome is not None
    assert r.hearing_date == datetime(2026, 3, 10)


def test_fresno_403_ruling_ten_west() -> None:
    """Verify specific fields from the Ten-West ruling in Dept 403."""
    text = _extract_pdf_text(_load_bytes("fresno_403_20260310_d019042f.pdf"))
    rulings = _split_rulings(text)
    ten_west = [r for r in rulings if r.case_title and "Ten-West" in r.case_title]
    assert len(ten_west) >= 1
    r = ten_west[0]
    assert r.case_number == "25CECG02662"
    assert "Default Prove-Up" in (r.motion_type or "")
    assert r.outcome == "Granted"


def test_fresno_501_ruling_godines() -> None:
    """Verify specific fields from the Godines ruling in Dept 501."""
    text = _extract_pdf_text(_load_bytes("fresno_501_20260310_10fe3c7f.pdf"))
    rulings = _split_rulings(text)
    godines = [r for r in rulings if r.case_title and "Godines" in r.case_title]
    assert len(godines) >= 1
    r = godines[0]
    assert r.case_number == "23CECG05097"
    assert r.case_title == "Godines v. MVED, Inc."


def test_fresno_502_ruling_garcia() -> None:
    """Verify specific fields from the Garcia ruling in Dept 502."""
    text = _extract_pdf_text(_load_bytes("fresno_502_20260311_eef66c41.pdf"))
    rulings = _split_rulings(text)
    garcia = [r for r in rulings if r.case_title and "Garcia" in r.case_title]
    assert len(garcia) >= 1
    r = garcia[0]
    assert r.case_number is not None
    assert r.case_title is not None
    assert r.outcome is not None


# ---------------------------------------------------------------------------
# _normalize_outcome — additional branch coverage
# ---------------------------------------------------------------------------


def test_normalize_outcome_sustain_plain() -> None:
    """Plain 'To sustain' without leave qualifier."""
    assert _normalize_outcome("To sustain the demurrer.") == "Sustained"


def test_normalize_outcome_sustain_with_leave() -> None:
    """'To sustain ... with leave to amend'."""
    assert _normalize_outcome("To sustain with leave to amend.") == "Sustained with Leave to Amend"


def test_normalize_outcome_overrule() -> None:
    assert _normalize_outcome("To overrule the demurrer.") == "Overruled"


def test_normalize_outcome_stay() -> None:
    assert _normalize_outcome("To stay all proceedings.") == "Stayed"


def test_normalize_outcome_take_off_calendar() -> None:
    assert _normalize_outcome("To take the motion off calendar.") == "Off Calendar"


def test_normalize_outcome_period_truncation() -> None:
    """Short outcome with a period should truncate at the period."""
    result = _normalize_outcome("Some other outcome text. More detail here")
    assert result == "Some other outcome text"


def test_normalize_outcome_long_truncation() -> None:
    """Outcomes longer than 60 chars without a short period get truncated at 60."""
    long_text = (
        "To do something very unusual that doesn't match any pattern and keeps going further"
    )
    result = _normalize_outcome(long_text)
    assert len(result) <= 60


def test_normalize_outcome_raw_strip_fallback() -> None:
    """Short outcome without period returns as-is stripped."""
    assert _normalize_outcome("  Short ruling  ") == "Short ruling"


# ---------------------------------------------------------------------------
# _extract_outcome — fallback disposition paths
# ---------------------------------------------------------------------------


def test_fresno_outcome_fallback_overruled() -> None:
    """Fallback regex matches 'overruled' when Tentative Ruling block has no 'To ...'."""
    text = "Tentative Ruling:\nSee below.\nThe demurrer is overruled in its entirety."
    assert _extract_outcome(text) == "Overruled"


def test_fresno_outcome_fallback_sustained() -> None:
    text = "Tentative Ruling:\nSee below.\nThe objection is sustained."
    assert _extract_outcome(text) == "Sustained"


def test_fresno_outcome_fallback_moot() -> None:
    text = "Tentative Ruling:\nSee below.\nThe motion is moot."
    assert _extract_outcome(text) == "Moot"


def test_fresno_outcome_fallback_continued_to() -> None:
    """Fallback 'continued to' pattern."""
    text = "Tentative Ruling:\nSee below.\nThe hearing is continued to April 15, 2026."
    assert _extract_outcome(text) == "Continued"


def test_fresno_outcome_fallback_off_calendar() -> None:
    """Fallback 'off calendar' text detection."""
    text = "Tentative Ruling:\nSee below.\nThis matter is taken off calendar."
    assert _extract_outcome(text) == "Off Calendar"


def test_fresno_outcome_fallback_off_calendar_short() -> None:
    """Fallback 'off calendar' without 'taken'."""
    text = "Tentative Ruling:\nSee below.\nMatter is off calendar per stipulation."
    assert _extract_outcome(text) == "Off Calendar"


# ---------------------------------------------------------------------------
# _fresno_hearing_date_from_text — no-comma format
# ---------------------------------------------------------------------------


def test_fresno_hearing_date_from_text_no_comma() -> None:
    """Hearing date format without comma: 'March 10 2026'."""
    text = "Tentative Rulings for March 10 2026\nDepartment 403"
    dt = _fresno_hearing_date_from_text(text)
    assert dt == datetime(2026, 3, 10)


def test_fresno_hearing_date_from_text_none() -> None:
    """No date found returns None."""
    assert _fresno_hearing_date_from_text("No date here at all") is None


def test_fresno_hearing_date_invalid_date() -> None:
    """Invalid date string returns None (e.g. bad month spelling)."""
    text = "Tentative Rulings for Frobuary 42, 2026\nDepartment 403"
    assert _fresno_hearing_date_from_text(text) is None


# ---------------------------------------------------------------------------
# _extract_case_title — edge cases
# ---------------------------------------------------------------------------


def test_fresno_extract_case_title_empty_after_strip() -> None:
    """Re: line with text that collapses to empty returns None.

    The _extract_case_title function strips whitespace and returns None
    if the result is empty. We can't easily hit this with real "Re:" patterns
    since the regex captures the whole line, so we verify the function handles
    a title that's just whitespace on the same line.
    """
    # "Re:" followed by text on the same line that is captured by (.+?)
    # The (.+?) requires at least one char, so true-empty after Re: doesn't match.
    # Instead, test with the function's text[:500] limit:
    # Put "Re:" line beyond the 500-char search window
    long_prefix = "A" * 501
    text = f"{long_prefix}\nRe: Hidden Title\nCase No. 25CECG03271"
    assert _extract_case_title(text) is None


# ---------------------------------------------------------------------------
# _extract_motion_type — truncation edge case
# ---------------------------------------------------------------------------


def test_fresno_extract_motion_type_truncated_at_120() -> None:
    """Motion type longer than 120 chars gets truncated."""
    long_motion = "A" * 130
    text = f"Motion: {long_motion}\nTentative Ruling:"
    result = _extract_motion_type(text)
    assert result is not None
    assert len(result) <= 120


def test_fresno_extract_motion_type_stops_at_keyword() -> None:
    """Motion type extraction stops at known field markers like 'Tentative Ruling:'."""
    text = "Motion: Demurrer to Complaint\nTentative Ruling:\nTo grant the motion."
    result = _extract_motion_type(text)
    assert result == "Demurrer to Complaint"
    # "Tentative Ruling:" should NOT be part of the motion
    assert "Tentative" not in result


# ---------------------------------------------------------------------------
# _split_rulings — edge cases
# ---------------------------------------------------------------------------


def test_split_rulings_no_entries() -> None:
    """Text without numbered entries returns empty list."""
    assert _split_rulings("Just some normal text without any rulings") == []


def test_split_rulings_short_entry_skipped() -> None:
    """Entries with ruling text < 50 chars are skipped."""
    text = (
        "(1) Tentative Ruling\n"
        "Short.\n"
        "(2) Tentative Ruling\n"
        "Re: Smith v. Jones\n"
        "Superior Court Case No. 25CECG03271\n"
        "Hearing Date: March 10, 2026 (Dept. 403)\n"
        "Motion: Demurrer to Complaint\n"
        "Tentative Ruling:\n"
        "To grant. The demurrer is sustained without leave to amend.\n"
    )
    rulings = _split_rulings(text)
    # Only the second entry should appear (first is too short)
    assert len(rulings) == 1
    assert rulings[0].ruling_index == 2


def test_split_rulings_removes_page_footers() -> None:
    """Page number footers are stripped from ruling text."""
    text = (
        "(1) Tentative Ruling\n"
        "Re: Smith v. Jones\n"
        "Superior Court Case No. 25CECG03271\n"
        "Hearing Date: March 10, 2026 (Dept. 403)\n"
        "Motion: Demurrer to Complaint\n"
        "Tentative Ruling:\n"
        "To grant the motion. The court finds good cause.\n"
        "15\n"
    )
    rulings = _split_rulings(text)
    assert len(rulings) == 1
    # The trailing page number should be removed
    assert not rulings[0].ruling_text.rstrip().endswith("15")


def test_split_rulings_newline_between_number_and_tentative() -> None:
    """Match entry where number and 'Tentative Ruling' are on separate lines."""
    text = (
        "(1)\n"
        "Tentative Ruling\n"
        "Re: Alpha v. Beta Corp\n"
        "Superior Court Case No. 25CECG09999\n"
        "Hearing Date: March 10, 2026 (Dept. 501)\n"
        "Motion: Motion for Summary Judgment\n"
        "Tentative Ruling:\n"
        "To deny the motion. The court finds triable issues of material fact.\n"
    )
    rulings = _split_rulings(text)
    assert len(rulings) == 1
    assert rulings[0].ruling_index == 1
    assert rulings[0].case_title == "Alpha v. Beta Corp"


# ---------------------------------------------------------------------------
# SplitRuling — direct construction
# ---------------------------------------------------------------------------


def test_split_ruling_construction() -> None:
    """SplitRuling can be constructed with all fields."""
    r = SplitRuling(
        ruling_index=1,
        case_number="25CECG03271",
        ruling_text="Some ruling text here",
        case_title="Smith v. Jones",
        motion_type="Demurrer",
        outcome="Sustained",
        hearing_date=datetime(2026, 3, 10),
    )
    assert r.ruling_index == 1
    assert r.case_number == "25CECG03271"
    assert r.case_title == "Smith v. Jones"
    assert r.motion_type == "Demurrer"
    assert r.outcome == "Sustained"
    assert r.hearing_date == datetime(2026, 3, 10)


# ---------------------------------------------------------------------------
# _NO_RULINGS_RE and _ISSUED_BY_RE regex patterns
# ---------------------------------------------------------------------------


def test_no_rulings_regex_matches() -> None:
    """_NO_RULINGS_RE matches boilerplate 'No Tentative Rulings' text."""
    assert _NO_RULINGS_RE.search("No Tentative Rulings") is not None
    assert _NO_RULINGS_RE.search("  No Tentative Ruling for this date") is not None
    assert _NO_RULINGS_RE.search("no tentative rulings") is not None


def test_no_rulings_regex_no_match() -> None:
    assert _NO_RULINGS_RE.search("Tentative Ruling:\nTo grant.") is None


def test_issued_by_regex_matches() -> None:
    """_ISSUED_BY_RE extracts judge initials from 'Issued By:' line."""
    m = _ISSUED_BY_RE.search("Issued By: lmg on 3-9-26 .")
    assert m is not None
    assert m.group("initials") == "lmg"

    m2 = _ISSUED_BY_RE.search("Issued By: DTT on 3-10-26 .")
    assert m2 is not None
    assert m2.group("initials") == "DTT"


def test_issued_by_regex_no_match() -> None:
    assert _ISSUED_BY_RE.search("No issued by line") is None


# ---------------------------------------------------------------------------
# _fresno_courthouse
# ---------------------------------------------------------------------------


def test_fresno_courthouse_returns_constant() -> None:
    """_fresno_courthouse always returns the same courthouse name."""
    assert _fresno_courthouse("403") == "B.F. Sisk Federal Courthouse"
    assert _fresno_courthouse("501") == "B.F. Sisk Federal Courthouse"
    assert _fresno_courthouse("anything") == "B.F. Sisk Federal Courthouse"


# ---------------------------------------------------------------------------
# _fresno_hearing_date_from_filename — invalid date values
# ---------------------------------------------------------------------------


def test_fresno_hearing_date_from_filename_invalid_month() -> None:
    """Invalid month (13) returns None via ValueError."""
    assert _fresno_hearing_date_from_filename("13-10-26-dept-403.pdf") is None


def test_fresno_hearing_date_from_filename_invalid_day() -> None:
    """Invalid day (32) returns None via ValueError."""
    assert _fresno_hearing_date_from_filename("03-32-26-dept-403.pdf") is None


# ---------------------------------------------------------------------------
# FresnoTentativeRulingsScraper.parse_document — pre-split fallback path
# ---------------------------------------------------------------------------


@respx.mock
def test_fresno_parse_document_pre_split_no_hearing_date_fallback() -> None:
    """Pre-split doc without hearing_date falls back to filename extraction."""
    config = fresno_default_config()
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(config=config)

    from framework import ContentFormat

    doc = scraper._make_base_doc(
        source_url="https://example.com/03-10-26-dept-403.pdf",
        raw_content=b"fake-pdf",
        content_format=ContentFormat.PDF,
    )
    doc.extra["pre_split"] = True
    doc.extra["filename"] = "03-10-26-dept-403.pdf"
    doc.hearing_date = None

    parsed = scraper.parse_document(doc)
    assert parsed.hearing_date == datetime(2026, 3, 10)


@respx.mock
def test_fresno_parse_document_non_pre_split_path() -> None:
    """Non-pre-split doc goes through parent parse_document and Fresno-specific logic."""
    from unittest.mock import patch

    from framework import ContentFormat

    config = fresno_default_config()
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(config=config)

    doc = scraper._make_base_doc(
        source_url="https://example.com/03-10-26-dept-403.pdf",
        raw_content=b"fake-pdf",
        content_format=ContentFormat.PDF,
    )
    doc.extra["filename"] = "03-10-26-dept-403.pdf"

    # Mock _extract_pdf_text to return text with a hearing date
    with patch(
        "courts.ca.pdf_link_scraper._extract_pdf_text",
        return_value=(
            "Tentative Rulings for March 10, 2026\n"
            "Department 403\nCase No. 25CECG03271\nSome ruling text"
        ),
    ):
        parsed = scraper.parse_document(doc)

    assert parsed.hearing_date == datetime(2026, 3, 10)
    assert parsed.case_number is not None


@respx.mock
def test_fresno_parse_document_non_pre_split_filename_fallback() -> None:
    """Non-pre-split doc falls back to filename for hearing date when text has none."""
    from unittest.mock import patch

    from framework import ContentFormat

    config = fresno_default_config()
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(config=config)

    doc = scraper._make_base_doc(
        source_url="https://example.com/03-10-26-dept-403.pdf",
        raw_content=b"fake-pdf",
        content_format=ContentFormat.PDF,
    )
    doc.extra["filename"] = "03-10-26-dept-403.pdf"

    # Mock _extract_pdf_text to return text without any date
    with patch(
        "courts.ca.pdf_link_scraper._extract_pdf_text",
        return_value="Some ruling text without a date. Case No. 25CECG03271",
    ):
        parsed = scraper.parse_document(doc)

    # Should fall back to filename for hearing date
    assert parsed.hearing_date == datetime(2026, 3, 10)


# ---------------------------------------------------------------------------
# fetch_documents — PDF text extraction failure path
# ---------------------------------------------------------------------------


@respx.mock
def test_fresno_fetch_documents_pdf_extraction_failure() -> None:
    """When PDF text extraction fails, the original doc is kept."""
    from unittest.mock import patch

    html = _load_html("fresno_index_page.html")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    # Return invalid PDF bytes to trigger extraction failure
    respx.get(url__regex=r"\.pdf").mock(
        return_value=httpx.Response(200, content=b"not-a-valid-pdf")
    )

    config = fresno_default_config()
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(config=config)

    with patch(
        "courts.ca.fresno_tentatives._extract_pdf_text",
        side_effect=Exception("PDF extraction failed"),
    ):
        docs = scraper.fetch_documents()

    # All 20 PDFs should still produce docs (kept as-is on failure)
    assert len(docs) == 20


# ---------------------------------------------------------------------------
# fetch_documents — single ruling PDF (<=1 rulings)
# ---------------------------------------------------------------------------


@respx.mock
def test_fresno_fetch_documents_single_ruling_pdf() -> None:
    """A PDF with exactly 1 ruling keeps the original doc with fields populated."""
    from unittest.mock import patch

    html = _load_html("fresno_index_page.html")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf").mock(return_value=httpx.Response(200, content=b"fake-pdf-bytes"))

    single_ruling_text = (
        "(1) Tentative Ruling\n"
        "Re: Smith v. Jones\n"
        "Superior Court Case No. 25CECG03271\n"
        "Hearing Date: March 10, 2026 (Dept. 403)\n"
        "Motion: Demurrer to Complaint\n"
        "Tentative Ruling:\n"
        "To grant the motion. The court finds good cause for the relief requested.\n"
    )

    with patch(
        "courts.ca.fresno_tentatives._extract_pdf_text",
        return_value=single_ruling_text,
    ):
        config = fresno_default_config()
        config.request_delay_seconds = 0
        scraper = FresnoTentativeRulingsScraper(config=config)
        docs = scraper.fetch_documents()

    # Verify the docs got field data from the single ruling
    assert len(docs) == 20
    assert docs[0].case_number == "25CECG03271"
    assert docs[0].case_title == "Smith v. Jones"


# ---------------------------------------------------------------------------
# Motions with (xN) count pattern
# ---------------------------------------------------------------------------


def test_fresno_extract_motion_with_count() -> None:
    """Motion with count like 'Motions (x3):'."""
    text = "Motions (x3): Motion to Compel Responses\nTentative Ruling:"
    result = _extract_motion_type(text)
    assert result is not None
    assert "Compel" in result


# ---------------------------------------------------------------------------
# fetch_documents — zero rulings from split
# ---------------------------------------------------------------------------


@respx.mock
def test_fresno_fetch_documents_zero_rulings() -> None:
    """A PDF with no numbered rulings keeps the original doc."""
    from unittest.mock import patch

    html = _load_html("fresno_index_page.html")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf").mock(return_value=httpx.Response(200, content=b"fake-pdf-bytes"))

    with patch(
        "courts.ca.fresno_tentatives._extract_pdf_text",
        return_value="No Tentative Rulings for this date.",
    ):
        config = fresno_default_config()
        config.request_delay_seconds = 0
        scraper = FresnoTentativeRulingsScraper(config=config)
        docs = scraper.fetch_documents()

    # All 20 PDFs produce 1 doc each (no splitting since no rulings found)
    assert len(docs) == 20
