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
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from botocore.exceptions import ClientError

from courts.ca.sc_tentatives import (
    COURT_ID,
    LANDING_URL,
    SantaClaraCourtDirectory,
    SCTentativeRulingsScraper,
    SplitRuling,
    _split_rulings,
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
from framework.hashing import sha256_hex

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


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
    """All extracted case numbers should match the expected format (CV or PR prefix)."""
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    case_numbers = parse_all_case_numbers(text)
    import re

    for cn in case_numbers:
        assert re.match(r"\d{2}(?:CV|PR)\d{6}$", cn), f"Unexpected format: {cn}"


def test_sc_case_number_probate_format() -> None:
    """parse_case_number should recognize probate (PR) case numbers."""
    assert parse_case_number("Case No.: 25PR199782") == "25PR199782"
    assert parse_case_number("25PR200035") == "25PR200035"


def test_sc_case_numbers_mixed_cv_and_pr() -> None:
    """parse_all_case_numbers should find both CV and PR format numbers."""
    text = "LINE 1 24CV443183 Smith v Jones  Demurrer\nLINE 2 25PR199782 Estate of Doe  Petition"
    case_numbers = parse_all_case_numbers(text)
    assert "24CV443183" in case_numbers
    assert "25PR199782" in case_numbers
    assert len(case_numbers) == 2


def test_sc_case_number_probate_not_other_prefixes() -> None:
    """Only CV and PR prefixes should be matched, not arbitrary two-letter codes."""
    assert parse_case_number("25XX199782") is None
    assert parse_case_number("25AB199782") is None


def test_sc_case_number_case_insensitive() -> None:
    """parse_case_number should match lowercase input and return uppercase."""
    assert parse_case_number("25pr199782") == "25PR199782"
    assert parse_case_number("24cv443183") == "24CV443183"
    assert parse_case_number("25Pr199782") == "25PR199782"


def test_sc_case_numbers_case_insensitive() -> None:
    """parse_all_case_numbers should match lowercase input and return uppercase."""
    result = parse_all_case_numbers("24cv443183 25pr199782")
    assert result == ["24CV443183", "25PR199782"]


# ---------------------------------------------------------------------------
# Outcome extraction
# ---------------------------------------------------------------------------


def test_sc_outcome_granted() -> None:
    # parse_outcome now returns DB-compatible lowercase enum values (#2113)
    assert parse_outcome("Plaintiff's motion is GRANTED.") == "granted"


def test_sc_outcome_denied() -> None:
    assert parse_outcome("The motion is DENIED.") == "denied"


def test_sc_outcome_off_calendar() -> None:
    assert parse_outcome("This matter is OFF calendar.") == "off_calendar"
    assert parse_outcome("Case is off calendar.") == "off_calendar"


def test_sc_outcome_sustained() -> None:
    # SUSTAINED maps to "granted" in the ruling_outcome enum
    assert parse_outcome("Defendant's demurrer is SUSTAINED.") == "granted"


def test_sc_outcome_overruled() -> None:
    # OVERRULED maps to "denied" in the ruling_outcome enum
    assert parse_outcome("Demurrer is OVERRULED.") == "denied"


def test_sc_outcome_moot() -> None:
    assert parse_outcome("The motion is rendered MOOT.") == "moot"


def test_sc_outcome_none_for_empty() -> None:
    assert parse_outcome("") is None
    assert parse_outcome("No outcome here") is None


def test_sc_outcome_from_real_pdf_dept6() -> None:
    text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
    outcome = parse_outcome(text)
    assert outcome is not None
    # Values should now be valid ruling_outcome enum values (#2113)
    assert outcome in ("off_calendar", "granted", "denied", "moot")


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


def test_sc_case_title_case_name_field() -> None:
    """Strategy 1: 'Case Name:' field (Dept 7 probate format)."""
    text = "Tentative Ruling\nCase Name: The Estate of Anthony Intravaia\nCase No.: 25PR199782\n"
    assert parse_case_title(text) == "The Estate of Anthony Intravaia"


def test_sc_case_title_line_entry_with_dashes() -> None:
    """Strategy 2: 'LINE N - CASENO – Title' in ruling body."""
    text = (
        "LINE # CASE # CASE TITLE RULING\n"
        "LINE 1 24PR196490 In re the Klein Living Trust summary text\n"
        "SUPERIOR COURT\n"
        "LINE 1 - 24PR196490 \u2013 In re the Klein Living Trust Dated 6/5/1980\n"
        "Petitioner cites Lee v. Swansboro (2007) 151 Cal.App.4th 575\n"
    )
    title = parse_case_title(text)
    assert title is not None
    assert "Klein Living Trust" in title
    assert "Petitioner" not in title
    assert "Swansboro" not in title


def test_sc_case_title_rejects_legal_citation() -> None:
    """The fallback vs_re must not match legal citations in ruling text."""
    text = (
        "The court's analysis begins.\n"
        "Petitioner cites Lee v. Swansboro Country Property Owners Assn. "
        "(2007) 151 Cal.App.4th 575\n"
        "for the proposition that trust modifications require consent.\n"
    )
    title = parse_case_title(text)
    # Should return None because the only "v." is a legal citation
    assert title is None


def test_sc_case_title_allows_real_vs_party_name() -> None:
    """Real party-vs-party titles should still match the fallback pattern."""
    text = "Smith vs Jones  Demurrer is GRANTED.\n"
    title = parse_case_title(text)
    assert title is not None
    assert "Smith" in title
    assert "Jones" in title


def test_sc_case_title_truncates_long_vs_match() -> None:
    """Fallback 'v.' pattern truncates matches longer than 200 characters."""
    long_plaintiff = "A" * 120
    long_defendant = "B" * 120
    text = f"{long_plaintiff} vs {long_defendant} Demurrer is GRANTED.\n"
    title = parse_case_title(text)
    assert title is not None
    assert len(title) == 200


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


# ---------------------------------------------------------------------------
# SantaClaraCourtDirectory — snapshot behavior
# ---------------------------------------------------------------------------


def _head_object_404(**kwargs: Any) -> None:
    """Simulate S3 HeadObject returning 404 (object not found)."""
    raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


def _mock_s3() -> MagicMock:
    """Create a mock S3 client with HeadObject returning 404 (not found)."""
    mock = MagicMock()
    mock.head_object.side_effect = _head_object_404
    return mock


def _mock_db_conn(existing_hash: str | None = None) -> MagicMock:
    """Create a mock psycopg connection with cursor context manager."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    # _is_duplicate check: return None (no prior snapshot) or the existing hash
    if existing_hash is not None:
        cursor.fetchone.return_value = (existing_hash,)
    else:
        cursor.fetchone.return_value = None
    return conn


class TestSantaClaraCourtDirectory:
    """Tests for the SantaClaraCourtDirectory subclass."""

    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        """fetch_current() should return raw HTML bytes and a dept-to-judge mapping."""
        landing_html = _load_html("sc_landing_page.html")
        respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))

        s3 = _mock_s3()
        db = _mock_db_conn()
        directory = SantaClaraCourtDirectory(s3_client=s3, s3_bucket="test-bucket", db_conn=db)

        raw, mapping = directory.fetch_current()

        assert isinstance(raw, bytes)
        assert len(raw) > 0
        # Should have entries for the 10 departments with judges
        assert len(mapping) >= 8  # some depts might not have judge links
        assert mapping.get("1") == "Eunice W. Lee"
        assert mapping.get("6") == "Rafael Sivilla-Jones"
        assert mapping.get("16") == "Vincent I. Parrett"

    @respx.mock
    def test_fetch_and_snapshot_archives_to_s3(self) -> None:
        """fetch_and_snapshot() should upload raw HTML to S3."""
        landing_html = _load_html("sc_landing_page.html")
        respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))

        s3 = _mock_s3()
        db = _mock_db_conn()
        directory = SantaClaraCourtDirectory(s3_client=s3, s3_bucket="test-bucket", db_conn=db)

        mapping = directory.fetch_and_snapshot(COURT_ID)

        assert len(mapping) >= 8
        s3.put_object.assert_called_once()
        call_kwargs = s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"].startswith(f"directories/{COURT_ID}/")
        assert call_kwargs["ContentType"] == "text/html"

    @respx.mock
    def test_fetch_and_snapshot_inserts_into_db(self) -> None:
        """fetch_and_snapshot() should insert a new snapshot row into the DB."""
        landing_html = _load_html("sc_landing_page.html")
        respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))

        s3 = _mock_s3()
        db = _mock_db_conn()
        directory = SantaClaraCourtDirectory(s3_client=s3, s3_bucket="test-bucket", db_conn=db)

        directory.fetch_and_snapshot(COURT_ID)

        db.commit.assert_called_once()
        cursor = db.cursor.return_value.__enter__.return_value
        calls = cursor.execute.call_args_list
        # First call: dedup SELECT, second call: INSERT
        assert len(calls) == 2
        insert_sql = calls[1].args[0]
        assert "INSERT INTO court_directory_snapshots" in insert_sql

    @respx.mock
    def test_fetch_and_snapshot_dedup_skips_insert(self) -> None:
        """When content hash matches, the DB insert should be skipped (dedup)."""
        landing_html = _load_html("sc_landing_page.html")
        respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))

        s3 = _mock_s3()

        # First, get the actual raw bytes the directory will return
        dir_probe = SantaClaraCourtDirectory(s3_client=s3, s3_bucket="b", db_conn=_mock_db_conn())
        actual_raw, _ = dir_probe.fetch_current()
        content_hash = sha256_hex(actual_raw)

        # Now create the real directory with the existing hash matching
        respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))
        s3_real = _mock_s3()
        db = _mock_db_conn(existing_hash=content_hash)
        directory = SantaClaraCourtDirectory(s3_client=s3_real, s3_bucket="test-bucket", db_conn=db)

        mapping = directory.fetch_and_snapshot(COURT_ID)

        # S3 upload happens because HeadObject returns 404 (content-addressed key not yet in S3)
        s3_real.put_object.assert_called_once()
        # But DB commit should NOT happen (dedup)
        db.commit.assert_not_called()
        # Mapping should still be returned
        assert len(mapping) >= 8

    @respx.mock
    def test_mapping_excludes_departments_without_judges(self) -> None:
        """Departments without judge names should not appear in the mapping."""
        landing_html = _load_html("sc_landing_page.html")
        respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))

        s3 = _mock_s3()
        db = _mock_db_conn()
        directory = SantaClaraCourtDirectory(s3_client=s3, s3_bucket="test-bucket", db_conn=db)

        _, mapping = directory.fetch_current()

        # All values should be non-empty judge names
        for dept, judge in mapping.items():
            assert judge, f"Department {dept} has empty judge name"
            assert isinstance(judge, str)


class TestSCScraperWithDirectory:
    """Tests for SCTentativeRulingsScraper wired to SantaClaraCourtDirectory."""

    @respx.mock
    def test_scraper_snapshots_directory_on_run(self) -> None:
        """When court_directory is provided, the scraper should call fetch_and_snapshot."""
        landing_html = _load_html("sc_landing_page.html")
        dept1_html = _load_html("sc_dept1_page.html")
        dept1_pdf = _load_bytes("sc_dept1_tues.pdf")

        respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))
        respx.get(url__regex=r"tentative-rulings/dep").mock(
            return_value=httpx.Response(200, text=dept1_html)
        )
        respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=dept1_pdf))

        s3 = _mock_s3()
        db = _mock_db_conn()
        directory = SantaClaraCourtDirectory(s3_client=s3, s3_bucket="test-bucket", db_conn=db)

        config = sc_default_config()
        config.request_delay_seconds = 0
        scraper = SCTentativeRulingsScraper(config=config, court_directory=directory)
        health = scraper.run()

        assert health.success is True
        # Directory should have been archived to S3
        s3.put_object.assert_called_once()
        call_kwargs = s3.put_object.call_args.kwargs
        assert call_kwargs["Key"].startswith(f"directories/{COURT_ID}/")

    @respx.mock
    def test_scraper_works_without_directory(self) -> None:
        """When no court_directory is provided, the scraper should still work (backward compat)."""
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
        # No court_directory — should fall back to inline behavior
        scraper = SCTentativeRulingsScraper(config=config)
        health = scraper.run()

        assert health.success is True
        assert health.records_captured == 20

    @respx.mock
    def test_scraper_with_directory_still_captures_all_docs(self) -> None:
        """Scraper with directory should still capture the same number of documents."""
        landing_html = _load_html("sc_landing_page.html")
        dept1_html = _load_html("sc_dept1_page.html")
        dept1_pdf = _load_bytes("sc_dept1_tues.pdf")

        respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))
        respx.get(url__regex=r"tentative-rulings/dep").mock(
            return_value=httpx.Response(200, text=dept1_html)
        )
        respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=dept1_pdf))

        s3 = _mock_s3()
        db = _mock_db_conn()
        directory = SantaClaraCourtDirectory(s3_client=s3, s3_bucket="test-bucket", db_conn=db)

        config = sc_default_config()
        config.request_delay_seconds = 0
        scraper = SCTentativeRulingsScraper(config=config, court_directory=directory)
        health = scraper.run()

        assert health.success is True
        # 10 departments x 2 PDFs each = 20 documents
        assert health.records_captured == 20


# ---------------------------------------------------------------------------
# Date-aware directory lookup in parse_document (#767)
# ---------------------------------------------------------------------------


class TestDateAwareDirectoryLookup:
    """Tests for date-aware directory snapshot usage in parse_document."""

    def _make_scraper_with_directory(
        self,
        snapshot_map: dict[str, str] | None = None,
        live_mapping: dict[str, str] | None = None,
    ) -> SCTentativeRulingsScraper:
        """Create a scraper with a mocked court_directory for testing."""
        from courts.ca.sc_tentatives import default_config as sc_cfg

        config = sc_cfg()
        config.request_delay_seconds = 0

        court_directory = MagicMock()
        court_directory.get_mapping_for_date.return_value = snapshot_map

        scraper = SCTentativeRulingsScraper(config=config, court_directory=court_directory)
        scraper._dir_mapping = live_mapping or {}
        return scraper

    def test_uses_date_appropriate_snapshot_when_judge_missing(self) -> None:
        """parse_document should use historical snapshot when judge is None after PDF parse."""
        from framework import CapturedDocument, ContentFormat

        snapshot_map = {"1": "Historical Judge"}
        scraper = self._make_scraper_with_directory(
            snapshot_map=snapshot_map,
            live_mapping={"1": "Live Judge"},
        )

        # Create a doc with no judge_name (simulating PDF without judge header)
        doc = CapturedDocument(
            scraper_id="ca-sc-tentatives-civil",
            state="CA",
            county="Santa Clara",
            court="Superior Court",
            source_url="https://example.com/dept-1-tues.pdf",
            capture_timestamp=datetime(2026, 3, 3),
            content_format=ContentFormat.PDF,
            raw_content=b"%PDF-1.4 empty",  # Invalid PDF, will fail parse
            content_hash="",
            department="1",
            hearing_date=datetime(2026, 1, 15),
        )
        result = scraper.parse_document(doc)
        assert result.judge_name == "Historical Judge"

        scraper._court_directory.get_mapping_for_date.assert_called_once_with(
            COURT_ID,
            datetime(2026, 1, 15),
            fallback={"1": "Live Judge"},
        )

    @respx.mock
    def test_skips_directory_lookup_when_judge_from_pdf(self) -> None:
        """When the PDF contains a judge name, directory lookup should be skipped."""
        from framework import CapturedDocument, ContentFormat

        dept1_pdf = _load_bytes("sc_dept1_tues.pdf")
        scraper = self._make_scraper_with_directory(
            snapshot_map={"1": "Wrong Judge"},
            live_mapping={"1": "Also Wrong"},
        )

        doc = CapturedDocument(
            scraper_id="ca-sc-tentatives-civil",
            state="CA",
            county="Santa Clara",
            court="Superior Court",
            source_url="https://example.com/dept-1-tues.pdf",
            capture_timestamp=datetime(2026, 3, 3),
            content_format=ContentFormat.PDF,
            raw_content=dept1_pdf,
            content_hash="",
            department="1",
            hearing_date=datetime(2026, 3, 3),
        )
        result = scraper.parse_document(doc)
        # Judge should come from the PDF, not directory
        assert result.judge_name == "Eunice Lee"
        scraper._court_directory.get_mapping_for_date.assert_not_called()

    def test_falls_back_to_live_mapping_without_hearing_date(self) -> None:
        """Without hearing_date, should use _dir_mapping for fallback."""
        from framework import CapturedDocument, ContentFormat

        scraper = self._make_scraper_with_directory(
            snapshot_map={"1": "Snapshot Judge"},
            live_mapping={"1": "Live Judge"},
        )

        doc = CapturedDocument(
            scraper_id="ca-sc-tentatives-civil",
            state="CA",
            county="Santa Clara",
            court="Superior Court",
            source_url="https://example.com/dept-1-tues.pdf",
            capture_timestamp=datetime(2026, 3, 3),
            content_format=ContentFormat.PDF,
            raw_content=b"%PDF-1.4 empty",
            content_hash="",
            department="1",
        )
        result = scraper.parse_document(doc)
        assert result.judge_name == "Live Judge"
        scraper._court_directory.get_mapping_for_date.assert_not_called()


# ---------------------------------------------------------------------------
# _split_rulings — Santa Clara multi-case PDF deterministic splitter (#4303)
# ---------------------------------------------------------------------------
#
# Regression coverage for the carry-forward bug documented in #4303.  The
# Riverside splitter (#3649) eliminated this same class of bug for Riverside
# PDFs by splitting before LLM enrichment so each entry's enrichment runs
# against only its own text.  These tests pin the Santa Clara splitter to
# the same contract: each ``Line N``-bounded entry is a separate
# SplitRuling, the page-1 preamble is dropped, trailing index-only labels
# (``Line 10``..``Line 15`` at the end of the calendar) are skipped, and
# compact summary-table PDFs and single-ruling PDFs return ``[]`` or a
# 1-element list so the worker falls through to the LLM path.


class TestSantaClaraPdfSplit:
    """Unit tests for _split_rulings against real Santa Clara fixture PDFs."""

    def test_santa_clara_pdf_split_dept16_returns_nine_rulings(self) -> None:
        """sc_dept16_wed.pdf has 9 valid expanded-format entries (Lines 1-9)
        plus 6 trailing index labels (Lines 10-15) without bodies — the
        splitter must return 9 rulings and skip the trailing labels.
        """
        text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
        rulings = _split_rulings(text)
        assert len(rulings) == 9
        # Indices match the PDF's printed numbering 1..9.
        assert [r.ruling_index for r in rulings] == [1, 2, 3, 4, 5, 6, 7, 8, 9]

    def test_santa_clara_pdf_split_dept16_case_numbers_match_pdf(self) -> None:
        """Each split must carry the case_number from its own ``Case No.:``
        header.  Ground truth is from the fixture PDF's per-entry headers."""
        text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
        rulings = _split_rulings(text)
        case_numbers = [r.case_number for r in rulings]
        # Ground truth from the fixture (verified via probe).  Lines 3+4
        # share a case (consolidated motions on Ford Motor Company suit),
        # Lines 6+7 share a case (joined motion + cross-reference) — these
        # duplicates are CORRECT, not carry-forward leakage.
        assert case_numbers == [
            "24CV448974",  # Line 1: Wells Fargo v. Bagdriwicz
            "24CV445397",  # Line 2: Kwong v. Yang
            "24CV447738",  # Line 3: Jara Jurado v. Ford
            "24CV447738",  # Line 4: Jara Jurado v. Ford (consolidated)
            "25CV464528",  # Line 5: Vora v. Sunnyvale Gardens
            "24CV439631",  # Line 6: Ben Abdallah v. Fackler
            "24CV439631",  # Line 7: Ben Abdallah v. Fackler (joined)
            "23CV413738",  # Line 8: Emerging Growth Staffing v. DFO
            "25CV456254",  # Line 9: Hogan v. Moore
        ]

    def test_santa_clara_pdf_split_dept16_case_titles_per_entry(self) -> None:
        """Each split must carry its own ``case_title`` — the carry-forward
        fingerprint is identical case_titles across distinct case_numbers,
        so this test is the primary regression for #4303.

        Lines 1, 2, 3, 5, 6, 8, 9 all have distinct case_numbers, so their
        case_titles must all be distinct from each other.  Lines 3+4 and
        6+7 share case_numbers and SHOULD share titles (legitimate
        consolidation, not carry-forward).
        """
        text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
        rulings = _split_rulings(text)
        # Build {case_number: case_title} mapping; every distinct
        # case_number must have exactly one distinct case_title.
        cn_to_title: dict[str, set[str]] = {}
        for r in rulings:
            assert r.case_number is not None
            assert r.case_title is not None
            cn_to_title.setdefault(r.case_number, set()).add(r.case_title)
        for case_number, titles in cn_to_title.items():
            assert len(titles) == 1, (
                f"case_number {case_number} has {len(titles)} distinct titles: {titles}"
            )
        # And distinct case_numbers should mostly have distinct titles —
        # the 7 distinct case_numbers in dept16 must produce 7 distinct
        # titles (no leakage across cases).
        distinct_titles = {next(iter(ts)) for ts in cn_to_title.values()}
        assert len(distinct_titles) == len(cn_to_title), (
            "Distinct case_numbers must produce distinct case_titles "
            f"(got {len(distinct_titles)} titles for {len(cn_to_title)} case_numbers)"
        )

    def test_santa_clara_pdf_split_dept16_entry_text_isolation(self) -> None:
        """Entry N's ruling_text must contain only its own header + body —
        no cross-entry contamination from other entries' headers/bodies.

        This is the core invariant the splitter must guarantee: the LLM
        that runs against ``ruling_text`` should never see another
        entry's text, which is what enables it to violate the anti-
        carry-forward rule (5b in the per-county system prompt).
        """
        text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
        rulings = _split_rulings(text)
        # Pick three entries with clearly distinct parties for cross-
        # entry leakage assertions:
        #   Line 1: Wells Fargo / Bagdriwicz   (24CV448974)
        #   Line 2: Kwong / Yang               (24CV445397)
        #   Line 8: Emerging Growth / DFO      (23CV413738)
        by_idx = {r.ruling_index: r for r in rulings}
        r1, r2, r8 = by_idx[1], by_idx[2], by_idx[8]
        assert "Wells Fargo" in r1.ruling_text
        assert "Bagdriwicz" in r1.ruling_text
        assert "Kwong" not in r1.ruling_text
        assert "Emerging Growth" not in r1.ruling_text
        assert "Kwong" in r2.ruling_text
        assert "Yang" in r2.ruling_text
        assert "Wells Fargo" not in r2.ruling_text
        assert "Emerging Growth" in r8.ruling_text
        assert "DFO" in r8.ruling_text
        assert "Wells Fargo" not in r8.ruling_text
        assert "Kwong" not in r8.ruling_text

    def test_santa_clara_pdf_split_dept16_preamble_is_dropped(self) -> None:
        """Page-1 boilerplate (UDC video instructions, courtroom rules,
        scheduling notice) must NOT appear in any per-entry ruling_text.
        """
        text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
        rulings = _split_rulings(text)
        for r in rulings:
            # Page-1 preamble markers from the dept 16 fixture.
            assert "Unicorn Digital Courtroom" not in r.ruling_text, (
                f"Entry {r.ruling_index} contains preamble UDC text"
            )
            assert "phone-only appearances" not in r.ruling_text.lower(), (
                f"Entry {r.ruling_index} contains preamble phone-only text"
            )

    def test_santa_clara_pdf_split_dept16_skips_trailing_index_labels(self) -> None:
        """Trailing ``Line 10``..``Line 15`` labels at the end of the
        dept 16 fixture have no body — they are calendar lines that were
        cleared after publication.  The splitter must skip them rather
        than emitting empty SplitRuling objects.
        """
        text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
        rulings = _split_rulings(text)
        # No ruling indexed 10 or above — those are trailing labels.
        ruling_indices = [r.ruling_index for r in rulings]
        for idx in ruling_indices:
            assert idx < 10, f"trailing index label Line {idx} was not skipped"

    def test_santa_clara_pdf_split_dept6_compact_table_falls_through(self) -> None:
        """sc_dept6_tues.pdf uses the compact summary-table format (no per-
        entry ``Line N`` boundaries on their own line).  The splitter
        returns at most 1 entry, so the worker falls through to the LLM
        path and the existing carry-forward protection (the LLM prompt's
        rule 5b) is the only defense for this format.

        This test pins the fall-through behavior so the splitter's
        coverage gap is explicit.  A future PR can extend the splitter
        to handle compact tables; until then, dept 6's residual carry-
        forward risk is acknowledged here.
        """
        text = extract_pdf_text(_load_bytes("sc_dept6_tues.pdf"))
        rulings = _split_rulings(text)
        # 0 or 1 entries — both trigger the worker's fall-through-to-LLM path.
        assert len(rulings) <= 1

    def test_santa_clara_pdf_split_dept1_summary_table_falls_through(self) -> None:
        """sc_dept1_tues.pdf uses ``LINE 1 CASENO TITLE ...`` on a single
        line (summary-table format) plus a single ``Calendar Line # 3 - 4``
        body section — neither matches the bare ``Line N`` boundary
        pattern.  The splitter returns ``[]`` so the worker falls through.
        """
        text = extract_pdf_text(_load_bytes("sc_dept1_tues.pdf"))
        rulings = _split_rulings(text)
        assert rulings == []

    def test_santa_clara_pdf_split_empty_text_returns_empty(self) -> None:
        """Empty text returns an empty list (defensive)."""
        assert _split_rulings("") == []

    def test_santa_clara_pdf_split_no_line_boundaries_returns_empty(self) -> None:
        """Text without any ``Line N`` boundaries returns empty (defensive)."""
        text = "Some random text without any line N boundaries"
        assert _split_rulings(text) == []

    def test_santa_clara_pdf_split_skips_too_short_entries(self) -> None:
        """Entries whose body is shorter than the threshold are skipped.

        This is the same defense that lets the dept 16 splitter ignore
        the trailing ``Line 10``..``Line 15`` labels — those have only
        a few chars of page-footer noise after them and must not be
        emitted as empty SplitRuling objects.
        """
        text = (
            "Line 1\n"
            "Short stub.\n"
            "Line 2\n"
            "Case Name: Foo v. Bar\n"
            "Case No.: 25CV111111\n"
            "Plaintiff Foo moves for summary judgment.  The motion is granted "
            "based on the undisputed evidence and applicable law.  This is a "
            "long-enough body to clear the minimum-entry threshold and be "
            "emitted as a real SplitRuling.\n"
        )
        rulings = _split_rulings(text)
        # Entry 1 is below the threshold; only entry 2 survives.
        assert len(rulings) == 1
        assert rulings[0].ruling_index == 2
        assert rulings[0].case_number == "25CV111111"
        assert rulings[0].case_title == "Foo v. Bar"

    def test_santa_clara_pdf_split_two_digit_entry_numbers(self) -> None:
        """Entry indices can be 1-3 digits (defensive — Santa Clara
        calendars sometimes run >9 entries, e.g. dept 16's calendar
        labels Line 10..15 even when only 9 are ruled on).
        """
        body = (
            "Case Name: Foo v. Bar\n"
            "Case No.: 25CV111111\n"
            "The motion is granted based on the undisputed evidence and "
            "applicable law.  This body is long enough to clear the "
            "minimum-entry threshold.\n"
        )
        body_alpha = body.replace("Foo v. Bar", "Alpha v. Beta").replace("25CV111111", "25CV222222")
        body_gamma = body.replace("Foo v. Bar", "Gamma v. Delta").replace(
            "25CV111111", "25CV333333"
        )
        text = f"Line 9\n{body}\nLine 10\n{body_alpha}\nLine 11\n{body_gamma}\n"
        rulings = _split_rulings(text)
        assert [r.ruling_index for r in rulings] == [9, 10, 11]
        assert [r.case_number for r in rulings] == [
            "25CV111111",
            "25CV222222",
            "25CV333333",
        ]

    def test_santa_clara_pdf_split_handles_missing_case_no_header(self) -> None:
        """Entries without a structured ``Case No.:`` header must still be
        emitted (case_number=None) so the worker's per-entry LLM
        enrichment can fill it in.  This defends against future
        Santa Clara format variants that drop the header.
        """
        text = (
            "Line 1\n"
            "Plaintiff Foo moves for summary judgment.  The motion is granted "
            "based on the undisputed evidence and applicable law.  This body "
            "has no Case No. header.\n"
        )
        rulings = _split_rulings(text)
        assert len(rulings) == 1
        assert rulings[0].case_number is None
        assert rulings[0].case_title is None
        assert "summary judgment" in rulings[0].ruling_text

    def test_santa_clara_pdf_split_does_not_extract_motion_or_outcome(self) -> None:
        """The Santa Clara splitter intentionally leaves motion_type and
        outcome as ``None`` — those are populated by per-entry LLM
        enrichment via ``_llm_enrich_fields``.

        This is the same behavioural choice as the Riverside splitter
        (#3649): per-entry LLM enrichment runs against only the entry's
        own text, so the LLM can never carry-forward motion_type /
        outcome from one entry onto another.
        """
        text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
        rulings = _split_rulings(text)
        assert len(rulings) > 0
        for r in rulings:
            assert r.motion_type is None, "splitter must not populate motion_type"
            assert r.outcome is None, "splitter must not populate outcome"

    def test_santa_clara_pdf_split_returns_split_ruling_instances(self) -> None:
        """Splitter must return ``SplitRuling`` instances so the worker's
        ``_try_sc_pdf_split`` dispatcher can read the typed attributes."""
        text = extract_pdf_text(_load_bytes("sc_dept16_wed.pdf"))
        rulings = _split_rulings(text)
        for r in rulings:
            assert isinstance(r, SplitRuling)
            # Required attributes per SplitRuling.__slots__.
            assert hasattr(r, "ruling_index")
            assert hasattr(r, "case_number")
            assert hasattr(r, "ruling_text")
            assert hasattr(r, "case_title")
            assert hasattr(r, "motion_type")
            assert hasattr(r, "outcome")
            assert hasattr(r, "hearing_date")
            assert hasattr(r, "department")
