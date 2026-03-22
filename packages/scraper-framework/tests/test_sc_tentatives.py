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
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from courts.ca.sc_tentatives import (
    COURT_ID,
    LANDING_URL,
    SantaClaraCourtDirectory,
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
from framework.hashing import sha256_hex

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

        s3 = MagicMock()
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

        s3 = MagicMock()
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

        s3 = MagicMock()
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

        s3 = MagicMock()

        # First, get the actual raw bytes the directory will return
        dir_probe = SantaClaraCourtDirectory(s3_client=s3, s3_bucket="b", db_conn=_mock_db_conn())
        actual_raw, _ = dir_probe.fetch_current()
        content_hash = sha256_hex(actual_raw)

        # Now create the real directory with the existing hash matching
        respx.get(LANDING_URL).mock(return_value=httpx.Response(200, text=landing_html))
        s3_real = MagicMock()
        db = _mock_db_conn(existing_hash=content_hash)
        directory = SantaClaraCourtDirectory(s3_client=s3_real, s3_bucket="test-bucket", db_conn=db)

        mapping = directory.fetch_and_snapshot(COURT_ID)

        # S3 upload should still happen (always archive)
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

        s3 = MagicMock()
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

        s3 = MagicMock()
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

        s3 = MagicMock()
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
