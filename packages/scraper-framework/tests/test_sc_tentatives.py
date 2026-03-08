"""Tests for Santa Clara County tentative rulings scraper.

Fixtures captured from live site 2026-03-07:
  sc_landing_page.html  — landing page with 10 department links
  sc_dept1_page.html    — Department 1 page (2 PDF links: Tuesday, Thursday)
  sc_dept6_page.html    — Department 6 page (2 PDF links: Tuesday, Thursday)
  sc_dept16_page.html   — Department 16 page (2 PDF links: Wednesday, Friday)
  sc_dept1_tues.pdf     — Dept 1, Judge Eunice Lee, March 3, 2026 (7 pages)
  sc_dept6_tues.pdf     — Dept 6, Judge Rafael Sivilla-Jones, March 3, 2026 (13 pages)
  sc_dept16_wed.pdf     — Dept 16, Judge Vincent I. Parrett, March 4, 2026 (36 pages)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.sc_tentatives import (
    LANDING_URL,
    SCTentativeRulingsScraper,
    extract_departments,
    extract_pdf_links_from_dept_page,
    extract_pdf_text,
    parse_all_case_numbers,
    parse_case_number,
    parse_case_title,
    parse_department,
    parse_hearing_date,
    parse_judge_name,
    parse_motion_type,
    parse_outcome,
)
from courts.ca.sc_tentatives import default_config as sc_default_config

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# extract_departments — landing page parsing
# ---------------------------------------------------------------------------


def test_sc_extract_departments_count() -> None:
    html = _load_html("sc_landing_page.html")
    depts = extract_departments(html)
    assert len(depts) == 10


def test_sc_extract_departments_numbers() -> None:
    html = _load_html("sc_landing_page.html")
    depts = extract_departments(html)
    dept_numbers = sorted(d.department for d in depts)
    assert dept_numbers == sorted(["1", "2", "6", "7", "10", "12", "13", "16", "19", "22"])


def test_sc_extract_departments_judge_names() -> None:
    html = _load_html("sc_landing_page.html")
    depts = extract_departments(html)
    dept_map = {d.department: d.judge_name for d in depts}
    assert dept_map["1"] == "Eunice W. Lee"
    assert dept_map["2"] == "Amber Rosen"
    assert dept_map["6"] == "Rafael Sivilla-Jones"
    assert dept_map["16"] == "Vincent I. Parrett"
    assert dept_map["22"] == "Beth McGowan"


def test_sc_extract_departments_urls() -> None:
    html = _load_html("sc_landing_page.html")
    depts = extract_departments(html)
    dept_map = {d.department: d.page_url for d in depts}
    assert "department-1-tentative-rulings" in dept_map["1"]
    assert "dept-16-tentative-rulings" in dept_map["16"]


def test_sc_extract_departments_no_duplicates() -> None:
    html = _load_html("sc_landing_page.html")
    depts = extract_departments(html)
    dept_numbers = [d.department for d in depts]
    assert len(dept_numbers) == len(set(dept_numbers))


# ---------------------------------------------------------------------------
# extract_pdf_links_from_dept_page — department page parsing
# ---------------------------------------------------------------------------


def test_sc_dept1_pdf_links() -> None:
    html = _load_html("sc_dept1_page.html")
    links = extract_pdf_links_from_dept_page(html)
    assert len(links) == 2
    urls = [u for u, _ in links]
    assert any("dept-1-tues" in u for u in urls)
    assert any("dept-1-thurs" in u for u in urls)


def test_sc_dept6_pdf_links() -> None:
    html = _load_html("sc_dept6_page.html")
    links = extract_pdf_links_from_dept_page(html)
    assert len(links) == 2


def test_sc_dept16_pdf_links() -> None:
    html = _load_html("sc_dept16_page.html")
    links = extract_pdf_links_from_dept_page(html)
    assert len(links) == 2
    urls = [u for u, _ in links]
    assert any("dept-16-wed" in u for u in urls)
    assert any("dept-16-fri" in u for u in urls)


def test_sc_dept_page_excludes_rules_pdfs() -> None:
    """Court rules PDFs (civil_0.pdf, probate_1.pdf) should be excluded."""
    html = _load_html("sc_dept1_page.html")
    links = extract_pdf_links_from_dept_page(html)
    for url, _ in links:
        assert "/rules/" not in url


def test_sc_dept_pdf_links_absolute_urls() -> None:
    html = _load_html("sc_dept1_page.html")
    links = extract_pdf_links_from_dept_page(html)
    for url, _ in links:
        assert url.startswith("http")


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------


def test_sc_dept1_pdf_text_extraction() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept1_tues.pdf"))
    assert "Department 1" in text
    assert "Eunice" in text or "Lee" in text


def test_sc_dept6_pdf_text_extraction() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    assert "Department 6" in text
    assert "Sivilla-Jones" in text


def test_sc_dept16_pdf_text_extraction() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
    assert "Department 16" in text
    assert "Parrett" in text


# ---------------------------------------------------------------------------
# parse_judge_name — from PDF text
# ---------------------------------------------------------------------------


def test_sc_judge_dept1() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept1_tues.pdf"))
    assert parse_judge_name(text) == "Eunice Lee"


def test_sc_judge_dept6() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    assert parse_judge_name(text) == "Rafael Sivilla-Jones"


def test_sc_judge_dept16() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
    assert parse_judge_name(text) == "Vincent I. Parrett"


def test_sc_judge_returns_none_for_empty() -> None:
    assert parse_judge_name("") is None
    assert parse_judge_name("No judge info here") is None


# ---------------------------------------------------------------------------
# parse_department — from PDF text
# ---------------------------------------------------------------------------


def test_sc_department_from_pdf_dept1() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept1_tues.pdf"))
    assert parse_department(text) == "1"


def test_sc_department_from_pdf_dept6() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    assert parse_department(text) == "6"


def test_sc_department_from_pdf_dept16() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
    assert parse_department(text) == "16"


# ---------------------------------------------------------------------------
# parse_hearing_date — from PDF text
# ---------------------------------------------------------------------------


def test_sc_hearing_date_dept1() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept1_tues.pdf"))
    dt = parse_hearing_date(text)
    assert dt == datetime(2026, 3, 3)


def test_sc_hearing_date_dept6() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    dt = parse_hearing_date(text)
    assert dt == datetime(2026, 3, 3)


def test_sc_hearing_date_dept16() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
    dt = parse_hearing_date(text)
    assert dt == datetime(2026, 3, 4)


def test_sc_hearing_date_none_for_empty() -> None:
    assert parse_hearing_date("") is None
    assert parse_hearing_date("No date here") is None


# ---------------------------------------------------------------------------
# Case number extraction
# ---------------------------------------------------------------------------


def test_sc_case_number_dept1() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept1_tues.pdf"))
    cn = parse_case_number(text)
    assert cn is not None
    assert cn.startswith("2") and "CV" in cn


def test_sc_case_numbers_dept6() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    case_numbers = parse_all_case_numbers(text)
    assert len(case_numbers) >= 5
    assert "24CV443183" in case_numbers
    assert "25CV460465" in case_numbers


def test_sc_case_numbers_dept16() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
    case_numbers = parse_all_case_numbers(text)
    assert len(case_numbers) >= 3
    assert "23CV419582" in case_numbers


def test_sc_case_number_format() -> None:
    """All extracted case numbers should match the expected format."""
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    case_numbers = parse_all_case_numbers(text)
    import re

    for cn in case_numbers:
        assert re.match(r"\d{2}CV\d{6}$", cn), f"Unexpected format: {cn}"


# ---------------------------------------------------------------------------
# Outcome extraction
# ---------------------------------------------------------------------------


def test_sc_outcome_granted() -> None:
    assert parse_outcome("Plaintiff's motion is GRANTED.") == "GRANTED"


def test_sc_outcome_denied() -> None:
    assert parse_outcome("The motion is DENIED.") == "DENIED"


def test_sc_outcome_off_calendar() -> None:
    assert parse_outcome("This matter is OFF calendar.") == "Off calendar"
    assert parse_outcome("Case is off calendar.") == "Off calendar"


def test_sc_outcome_sustained() -> None:
    assert parse_outcome("Defendant's demurrer is SUSTAINED.") == "SUSTAINED"


def test_sc_outcome_overruled() -> None:
    assert parse_outcome("Demurrer is OVERRULED.") == "OVERRULED"


def test_sc_outcome_moot() -> None:
    assert parse_outcome("The motion is rendered MOOT.") == "MOOT"


def test_sc_outcome_none_for_empty() -> None:
    assert parse_outcome("") is None
    assert parse_outcome("No outcome here") is None


def test_sc_outcome_from_real_pdf_dept6() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    outcome = parse_outcome(text)
    assert outcome is not None
    assert outcome in ("Off calendar", "GRANTED", "DENIED", "SUSTAINED", "OVERRULED", "MOOT")


# ---------------------------------------------------------------------------
# Motion type extraction
# ---------------------------------------------------------------------------


def test_sc_motion_type_demurrer() -> None:
    assert parse_motion_type("Defendant moves for demurrer") == "Demurrer"


def test_sc_motion_type_summary_judgment() -> None:
    assert parse_motion_type("Plaintiff's Summary Judgment motion") == "Summary Judgment"


def test_sc_motion_type_compel_arbitration() -> None:
    result = parse_motion_type("Motion to Compel Arbitration and Stay")
    assert result is not None
    assert "Compel" in result


def test_sc_motion_type_from_real_pdf_dept6() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    motion = parse_motion_type(text)
    assert motion is not None


def test_sc_motion_type_none_for_empty() -> None:
    assert parse_motion_type("") is None
    assert parse_motion_type("No motion here") is None


# ---------------------------------------------------------------------------
# Case title extraction
# ---------------------------------------------------------------------------


def test_sc_case_title_from_real_pdf_dept6() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    title = parse_case_title(text)
    assert title is not None
    assert len(title) > 3


def test_sc_case_title_from_real_pdf_dept1() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept1_tues.pdf"))
    title = parse_case_title(text)
    assert title is not None


def test_sc_case_title_none_for_empty() -> None:
    assert parse_case_title("") is None


# ---------------------------------------------------------------------------
# Full scraper run — mocked HTTP using real fixtures
# ---------------------------------------------------------------------------


@respx.mock
def test_sc_full_run() -> None:
    """Full scraper run with mocked HTTP and real fixture data."""
    landing_html = _load_html("sc_landing_page.html")
    dept1_html = _load_html("sc_dept1_page.html")
    dept1_pdf = _load_bytes("sc_dept1_tues.pdf")

    # Mock landing page
    respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))
    # Mock all department pages with dept1 HTML
    respx.get(url__regex=r"tentative-rulings/dep").mock(
        return_value=httpx.Response(200, text=dept1_html)
    )
    # Mock all PDF downloads with dept1 PDF
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=dept1_pdf))

    config = sc_default_config()
    config.request_delay_seconds = 0
    scraper = SCTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    # 10 departments x 2 PDFs each = 20 documents
    assert health.records_captured == 20


@respx.mock
def test_sc_run_populates_all_fields() -> None:
    """Verify that parse_document populates all required fields."""
    landing_html = _load_html("sc_landing_page.html")
    dept6_html = _load_html("sc_dept6_page.html")
    dept6_pdf = _load_bytes("sc_dept6_tues.pdf")

    respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))
    respx.get(url__regex=r"tentative-rulings/dep").mock(
        return_value=httpx.Response(200, text=dept6_html)
    )
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=dept6_pdf))

    config = sc_default_config()
    config.request_delay_seconds = 0
    scraper = SCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    assert len(parsed) > 0
    first = parsed[0]

    # All required fields should be populated
    assert first.judge_name is not None, "judge_name should be populated"
    assert first.department is not None, "department should be populated"
    assert first.hearing_date is not None, "hearing_date should be populated"
    assert first.case_number is not None, "case_number should be populated"
    assert first.ruling_text is not None, "ruling_text should be populated"
    assert first.courthouse == "Downtown Superior Court"
    assert first.outcome is not None, "outcome should be populated"
    assert first.motion_type is not None, "motion_type should be populated"
    assert first.case_title is not None, "case_title should be populated"


@respx.mock
def test_sc_run_judge_from_pdf_refines_landing() -> None:
    """Judge name from PDF text should refine the landing page judge name."""
    landing_html = _load_html("sc_landing_page.html")
    dept1_html = _load_html("sc_dept1_page.html")
    dept1_pdf = _load_bytes("sc_dept1_tues.pdf")

    respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))
    respx.get(url__regex=r"tentative-rulings/dep").mock(
        return_value=httpx.Response(200, text=dept1_html)
    )
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=dept1_pdf))

    config = sc_default_config()
    config.request_delay_seconds = 0
    scraper = SCTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    # Before parsing, judge comes from landing page
    assert docs[0].judge_name == "Eunice W. Lee"

    parsed = [scraper.parse_document(d) for d in docs]
    # After parsing, judge name is refined from PDF (without middle initial)
    assert parsed[0].judge_name == "Eunice Lee"


@respx.mock
def test_sc_run_handles_landing_failure() -> None:
    respx.get(LANDING_URL).mock(return_value=httpx.Response(503))

    config = sc_default_config()
    config.max_retries = 1
    config.request_delay_seconds = 0
    scraper = SCTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0


@respx.mock
def test_sc_run_handles_dept_page_failure() -> None:
    """If one department page fails, other departments should still be scraped."""
    landing_html = _load_html("sc_landing_page.html")
    dept1_html = _load_html("sc_dept1_page.html")
    dept1_pdf = _load_bytes("sc_dept1_tues.pdf")

    respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))

    call_count = 0

    def dept_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(404)
        return httpx.Response(200, text=dept1_html)

    respx.get(url__regex=r"tentative-rulings/dep").mock(side_effect=dept_side_effect)
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=dept1_pdf))

    config = sc_default_config()
    config.request_delay_seconds = 0
    scraper = SCTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    # 9 departments succeeded (1 failed) x 2 PDFs = 18
    assert health.records_captured == 18


@respx.mock
def test_sc_run_handles_pdf_failure() -> None:
    """If one PDF download fails, other PDFs should still be captured."""
    landing_html = _load_html("sc_landing_page.html")
    dept1_html = _load_html("sc_dept1_page.html")
    dept1_pdf = _load_bytes("sc_dept1_tues.pdf")

    respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))
    respx.get(url__regex=r"tentative-rulings/dep").mock(
        return_value=httpx.Response(200, text=dept1_html)
    )

    call_count = 0

    def pdf_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(404)
        return httpx.Response(200, content=dept1_pdf)

    respx.get(url__regex=r"\.pdf$").mock(side_effect=pdf_side_effect)

    config = sc_default_config()
    config.request_delay_seconds = 0
    scraper = SCTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    # 20 PDFs total - 1 failed = 19
    assert health.records_captured == 19


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def test_sc_default_config() -> None:
    config = sc_default_config(s3_bucket="judgemind-document-archive-dev")
    assert config.scraper_id == "ca-sc-tentatives-civil"
    assert config.state == "CA"
    assert config.county == "Santa Clara"
    assert config.s3_bucket == "judgemind-document-archive-dev"
    assert len(config.schedule_windows) == 2
