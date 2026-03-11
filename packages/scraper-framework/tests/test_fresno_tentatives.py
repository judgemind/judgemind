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
    BASE_URL,
    INDEX_URL,
    FresnoTentativeRulingsScraper,
    _extract_case_number,
    _extract_case_title,
    _extract_motion_type,
    _extract_outcome,
    _fresno_dept_from_filename,
    _fresno_hearing_date_from_filename,
    _fresno_hearing_date_from_text,
    _split_rulings,
)
from courts.ca.fresno_tentatives import default_config as fresno_default_config
from courts.ca.pdf_link_scraper import _extract_pdf_links, _extract_pdf_text

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


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
