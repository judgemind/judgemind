"""Tests for San Francisco Family Law tentative rulings scraper.

Fixtures captured from live site 2026-03-03 (augmented with synthetic entries
for departments 405, 405A, 406, 425 per #2130):
  sf_family_law_page.html  — GET https://webapps.sftc.org/ufctr/ufctr.dll
                              (23 PDF links across Depts 403, 404, 405, 405A, 406, 416, 425)
  sf_dept403_ruling.pdf    — 403 Tentative Rulings 3.03.2026.pdf
                              Dept 403, Judge Bobby P. Luna, 30 pages
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.pdf_link_scraper import _extract_pdf_links, _extract_pdf_text
from courts.ca.sf_tentatives import (
    _LINK_TEXT_RE,
    BASE_URL,
    INDEX_URL,
    SFTentativeRulingsScraper,
    _sf_case_title_from_pdf_text,
    _sf_courthouse,
    _sf_hearing_date_from_filename,
    _sf_judge_from_pdf_text,
)
from courts.ca.sf_tentatives import default_config as sf_default_config

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# _extract_pdf_links — against real SF index page
# ---------------------------------------------------------------------------


def test_sf_extract_pdf_links_count() -> None:
    html = _load_html("sf_family_law_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    assert len(links) == 23


def test_sf_extract_pdf_links_absolute_urls() -> None:
    html = _load_html("sf_family_law_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    for url, _ in links:
        assert url.startswith("http"), f"Expected absolute URL, got {url!r}"
        assert ".pdf" in url.lower()


def test_sf_extract_pdf_links_no_duplicates() -> None:
    html = _load_html("sf_family_law_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    urls = [u for u, _ in links]
    assert len(urls) == len(set(urls))


def test_sf_extract_pdf_links_first_entry() -> None:
    html = _load_html("sf_family_law_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    url, text = links[0]
    assert "403" in url
    assert "Tentative Rulings" in text


def test_sf_extract_pdf_links_all_departments_present() -> None:
    html = _load_html("sf_family_law_page.html")
    links = _extract_pdf_links(html, INDEX_URL, BASE_URL)
    texts = [t for _, t in links]
    assert any("403" in t for t in texts)
    assert any("404" in t for t in texts)
    assert any("405A" in t for t in texts)
    assert any("406" in t for t in texts)
    assert any("416" in t for t in texts)
    assert any("425" in t for t in texts)


# ---------------------------------------------------------------------------
# _LINK_TEXT_RE — department regex tests (#2130)
# ---------------------------------------------------------------------------


def test_sf_link_text_re_three_digit_dept() -> None:
    """Standard 3-digit department like 403."""
    m = _LINK_TEXT_RE.match("403 Tentative Rulings 3.03.2026.pdf")
    assert m is not None
    assert m.group("department") == "403"


def test_sf_link_text_re_dept_with_letter_suffix() -> None:
    """Department with letter suffix like 405A (#2130)."""
    m = _LINK_TEXT_RE.match("405A Tentative Rulings 3.04.2026.pdf")
    assert m is not None
    assert m.group("department") == "405A"


def test_sf_link_text_re_dept_405() -> None:
    """Department 405 without suffix."""
    m = _LINK_TEXT_RE.match("405 Tentative Rulings 3.04.2026.pdf")
    assert m is not None
    assert m.group("department") == "405"


def test_sf_link_text_re_dept_425() -> None:
    """Department 425."""
    m = _LINK_TEXT_RE.match("425 Tentative Rulings 3.05.2026.pdf")
    assert m is not None
    assert m.group("department") == "425"


def test_sf_link_text_re_dept_406() -> None:
    """Department 406."""
    m = _LINK_TEXT_RE.match("406 Tentative Rulings 3.05.2026.pdf")
    assert m is not None
    assert m.group("department") == "406"


def test_sf_link_text_re_rejects_non_ruling() -> None:
    """Non-ruling filename should not match."""
    m = _LINK_TEXT_RE.match("some_other_document.pdf")
    assert m is None


# ---------------------------------------------------------------------------
# Hearing date extraction for departments with letter suffixes
# ---------------------------------------------------------------------------


def test_sf_hearing_date_dept_with_letter_suffix() -> None:
    """Hearing date extraction from 405A-style filename."""
    dt = _sf_hearing_date_from_filename("405A Tentative Rulings 3.04.2026.pdf")
    assert dt == datetime(2026, 3, 4)


# ---------------------------------------------------------------------------
# Courthouse mapping for new departments
# ---------------------------------------------------------------------------


def test_sf_courthouse_new_departments() -> None:
    """Departments 405, 405A, 406, 425 all map to the same courthouse."""
    assert _sf_courthouse("405") == "San Francisco Courthouse"
    assert _sf_courthouse("405A") == "San Francisco Courthouse"
    assert _sf_courthouse("406") == "San Francisco Courthouse"
    assert _sf_courthouse("425") == "San Francisco Courthouse"


# ---------------------------------------------------------------------------
# Full scraper run — dept 405A captured in mocked run (#2130)
# ---------------------------------------------------------------------------


@respx.mock
def test_sf_run_captures_dept_with_letter_suffix() -> None:
    """Verify department 405A is captured from the index page (#2130)."""
    html = _load_html("sf_family_law_page.html")
    pdf_bytes = _load_bytes("sf_dept403_ruling.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sf_default_config()
    config.request_delay_seconds = 0
    scraper = SFTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    departments = [d.department for d in docs]
    assert "405A" in departments
    assert "405" in departments
    assert "406" in departments
    assert "425" in departments


# ---------------------------------------------------------------------------
# _extract_pdf_text — against real SF fixture PDF
# ---------------------------------------------------------------------------


def test_sf_pdf_text_extraction() -> None:
    text = _extract_pdf_text(_load_bytes("sf_dept403_ruling.pdf"))
    assert "SAN FRANCISCO" in text
    assert "UNIFIED FAMILY COURT" in text


def test_sf_pdf_text_contains_case_numbers() -> None:
    import re

    text = _extract_pdf_text(_load_bytes("sf_dept403_ruling.pdf"))
    matches = re.findall(r"\bF[A-Z]{2}-\d{2}-\d{6}\b", text)
    assert len(matches) >= 1
    assert "FPT-25-378624" in matches


def test_sf_pdf_text_contains_judge_name() -> None:
    text = _extract_pdf_text(_load_bytes("sf_dept403_ruling.pdf"))
    assert "LUNA" in text


# ---------------------------------------------------------------------------
# _sf_judge_from_pdf_text
# ---------------------------------------------------------------------------


def test_sf_judge_from_pdf_text_presiding_format() -> None:
    text = _extract_pdf_text(_load_bytes("sf_dept403_ruling.pdf"))
    judge = _sf_judge_from_pdf_text(text)
    assert judge is not None
    assert "Luna" in judge


def test_sf_judge_title_cased() -> None:
    text = "Presiding: BOBBY P. LUNA\nsome other text"
    judge = _sf_judge_from_pdf_text(text)
    assert judge == "Bobby P. Luna"


def test_sf_judge_mixed_case_preserved() -> None:
    text = "Presiding: Bobby P. Luna\nsome other text"
    judge = _sf_judge_from_pdf_text(text)
    assert judge == "Bobby P. Luna"


def test_sf_judge_returns_none_for_empty_text() -> None:
    assert _sf_judge_from_pdf_text("") is None
    assert _sf_judge_from_pdf_text("No header here") is None


# ---------------------------------------------------------------------------
# _sf_hearing_date_from_filename
# ---------------------------------------------------------------------------


def test_sf_hearing_date_single_digit_month() -> None:
    dt = _sf_hearing_date_from_filename("403 Tentative Rulings 3.03.2026.pdf")
    assert dt == datetime(2026, 3, 3)


def test_sf_hearing_date_double_digit_month() -> None:
    dt = _sf_hearing_date_from_filename("404 Tentative Rulings 03.03.2026.pdf")
    assert dt == datetime(2026, 3, 3)


def test_sf_hearing_date_older_ruling() -> None:
    dt = _sf_hearing_date_from_filename("416 Tentative Rulings 12.11.2025.pdf")
    assert dt == datetime(2025, 12, 11)


def test_sf_hearing_date_returns_none_for_no_date() -> None:
    assert _sf_hearing_date_from_filename("some_random_file.pdf") is None


# ---------------------------------------------------------------------------
# Case number extraction — against real PDF
# ---------------------------------------------------------------------------


def test_sf_case_number_fpt_format() -> None:
    import re

    text = _extract_pdf_text(_load_bytes("sf_dept403_ruling.pdf"))
    matches = re.findall(r"\bF[A-Z]{2}-\d{2}-\d{6}\b", text)
    assert "FPT-25-378624" in matches


def test_sf_case_number_fms_format() -> None:
    import re

    text = _extract_pdf_text(_load_bytes("sf_dept403_ruling.pdf"))
    matches = re.findall(r"\bF[A-Z]{2}-\d{2}-\d{6}\b", text)
    assert "FMS-20-387302" in matches


def test_sf_case_number_fdi_format() -> None:
    import re

    text = _extract_pdf_text(_load_bytes("sf_dept403_ruling.pdf"))
    matches = re.findall(r"\bF[A-Z]{2}-\d{2}-\d{6}\b", text)
    assert "FDI-14-781786" in matches


def test_sf_multiple_case_numbers() -> None:
    import re

    text = _extract_pdf_text(_load_bytes("sf_dept403_ruling.pdf"))
    matches = list(dict.fromkeys(re.findall(r"\bF[A-Z]{2}-\d{2}-\d{6}\b", text)))
    # The 30-page PDF has multiple rulings with different case numbers
    assert len(matches) >= 3


# ---------------------------------------------------------------------------
# Courthouse mapping
# ---------------------------------------------------------------------------


def test_sf_courthouse_all_departments() -> None:
    assert _sf_courthouse("403") == "San Francisco Courthouse"
    assert _sf_courthouse("404") == "San Francisco Courthouse"
    assert _sf_courthouse("416") == "San Francisco Courthouse"


def test_sf_courthouse_unknown_department() -> None:
    # All SF family law departments map to the same courthouse
    assert _sf_courthouse("999") == "San Francisco Courthouse"


# ---------------------------------------------------------------------------
# Case title extraction — _sf_case_title_from_pdf_text
# ---------------------------------------------------------------------------


def test_sf_case_title_single_line_names() -> None:
    """Standard SF caption with single-line petitioner and respondent names."""
    text = (
        "1 SUPERIOR COURT OF CALIFORNIA\n"
        "2 COUNTY OF SAN FRANCISCO\n"
        "3 UNIFIED FAMILY COURT\n"
        "4\n"
        "5\n"
        ")\n"
        "6 MICHAEL EDWARD GRAVES, ) Case Number: FPT-25-378624\n"
        ")\n"
        "7 Petitioner ) Hearing Date: March 3, 2026\n"
        ")\n"
        "8 VS. ) Hearing Time: 9:00 AM\n"
        ")\n"
        "9 RANJIE LONG, ) Department: 403\n"
        ")\n"
        "10 Respondent ) Presiding: BOBBY P. LUNA\n"
    )
    title = _sf_case_title_from_pdf_text(text)
    assert title == "Michael Edward Graves v. Ranjie Long"


def test_sf_case_title_multi_line_name() -> None:
    """SF caption where petitioner name spans two lines."""
    text = (
        "1 SUPERIOR COURT OF CALIFORNIA\n"
        "2 COUNTY OF SAN FRANCISCO\n"
        "3 UNIFIED FAMILY COURT\n"
        "4\n"
        "5\n"
        ")\n"
        "6 MARIA DE LOS ANGELES RAMIREZ ) Case Number: FPT-25-378672\n"
        ")\n"
        "7 HERNANDEZ, ) Hearing Date: March 3, 2026\n"
        ")\n"
        "8 Petitioner ) Hearing Time: 9:00 AM\n"
        ")\n"
        "9 VS. ) Department: 403\n"
        ")\n"
        "10 EDILZAR FERNANDO JUAREZ MEJIA, ) Presiding: BOBBY P. LUNA\n"
        ")\n"
        "11 Respondent )\n"
    )
    title = _sf_case_title_from_pdf_text(text)
    assert title == "Maria De Los Angeles Ramirez Hernandez v. Edilzar Fernando Juarez Mejia"


def test_sf_case_title_from_fixture_pdf() -> None:
    """Extract case title from the real SF fixture PDF."""
    from courts.ca.pdf_link_scraper import _extract_pdf_text

    text = _extract_pdf_text(_load_bytes("sf_dept403_ruling.pdf"))
    title = _sf_case_title_from_pdf_text(text)
    assert title is not None
    assert "Graves" in title
    assert "Long" in title
    assert " v. " in title


def test_sf_case_title_returns_none_for_no_caption() -> None:
    assert _sf_case_title_from_pdf_text("") is None
    assert _sf_case_title_from_pdf_text("No caption block here") is None


def test_sf_case_title_title_cased() -> None:
    """All-caps names should be converted to title case."""
    text = (
        "1 SUPERIOR COURT OF CALIFORNIA\n"
        "2 COUNTY OF SAN FRANCISCO\n"
        "3 UNIFIED FAMILY COURT\n"
        "4\n"
        "5\n"
        ")\n"
        "6 JAMES PROPP, ) Case Number: FDI-14-781786\n"
        ")\n"
        "7 Petitioner ) Hearing Date: March 3, 2026\n"
        ")\n"
        "8 VS. ) Hearing Time: 9:00 AM\n"
        ")\n"
        "9 DENISE CHILDERS, ) Department: 403\n"
        ")\n"
        "10 Respondent ) Presiding: BOBBY P. LUNA\n"
    )
    title = _sf_case_title_from_pdf_text(text)
    assert title == "James Propp v. Denise Childers"


# ---------------------------------------------------------------------------
# Case title in full scraper run
# ---------------------------------------------------------------------------


@respx.mock
def test_sf_run_populates_case_title() -> None:
    html = _load_html("sf_family_law_page.html")
    pdf_bytes = _load_bytes("sf_dept403_ruling.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sf_default_config()
    config.request_delay_seconds = 0
    scraper = SFTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]
    has_title = [d for d in parsed if d.case_title]
    assert len(has_title) == 23
    assert "Graves" in has_title[0].case_title
    assert " v. " in has_title[0].case_title


# ---------------------------------------------------------------------------
# Full scraper run — mocked HTTP using real fixtures
# ---------------------------------------------------------------------------


@respx.mock
def test_sf_full_run() -> None:
    html = _load_html("sf_family_law_page.html")
    pdf_bytes = _load_bytes("sf_dept403_ruling.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sf_default_config()
    config.request_delay_seconds = 0
    scraper = SFTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 23


@respx.mock
def test_sf_run_populates_dept_from_filename() -> None:
    html = _load_html("sf_family_law_page.html")
    pdf_bytes = _load_bytes("sf_dept403_ruling.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sf_default_config()
    config.request_delay_seconds = 0
    scraper = SFTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    assert len(docs) == 23

    # First link is "403 Tentative Rulings 3.03.2026.pdf" → dept 403
    first = docs[0]
    assert first.department == "403"
    assert first.courthouse == "San Francisco Courthouse"


@respx.mock
def test_sf_run_populates_judge_from_pdf_text() -> None:
    html = _load_html("sf_family_law_page.html")
    pdf_bytes = _load_bytes("sf_dept403_ruling.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sf_default_config()
    config.request_delay_seconds = 0
    scraper = SFTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # Every doc should have judge name extracted from the PDF fixture
    has_judge = [d for d in parsed if d.judge_name]
    assert len(has_judge) == 23
    assert "Luna" in has_judge[0].judge_name


@respx.mock
def test_sf_run_populates_hearing_date() -> None:
    html = _load_html("sf_family_law_page.html")
    pdf_bytes = _load_bytes("sf_dept403_ruling.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sf_default_config()
    config.request_delay_seconds = 0
    scraper = SFTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]

    # All docs should have hearing dates parsed from filename
    has_date = [d for d in parsed if d.hearing_date]
    assert len(has_date) == 23


@respx.mock
def test_sf_run_extracts_case_numbers() -> None:
    html = _load_html("sf_family_law_page.html")
    pdf_bytes = _load_bytes("sf_dept403_ruling.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(url__regex=r"\.pdf$").mock(return_value=httpx.Response(200, content=pdf_bytes))

    config = sf_default_config()
    config.request_delay_seconds = 0
    scraper = SFTentativeRulingsScraper(config=config)

    docs = scraper.fetch_documents()
    parsed = [scraper.parse_document(d) for d in docs]
    has_case = [d for d in parsed if d.case_number]
    assert len(has_case) > 0
    assert has_case[0].case_number == "FPT-25-378624"


@respx.mock
def test_sf_run_handles_get_failure() -> None:
    respx.get(INDEX_URL).mock(return_value=httpx.Response(503))

    config = sf_default_config()
    config.max_retries = 1
    config.request_delay_seconds = 0
    scraper = SFTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0


@respx.mock
def test_sf_run_continues_when_pdf_fails() -> None:
    html = _load_html("sf_family_law_page.html")
    pdf_bytes = _load_bytes("sf_dept403_ruling.pdf")

    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))

    call_count = 0

    def pdf_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(404)
        return httpx.Response(200, content=pdf_bytes)

    respx.get(url__regex=r"\.pdf$").mock(side_effect=pdf_side_effect)

    config = sf_default_config()
    config.request_delay_seconds = 0
    scraper = SFTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 22  # 23 - 1 failed


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def test_sf_default_config() -> None:
    config = sf_default_config(s3_bucket="judgemind-document-archive-dev")
    assert config.scraper_id == "ca-sf-tentatives-family-law"
    assert config.state == "CA"
    assert config.county == "San Francisco"
    assert config.s3_bucket == "judgemind-document-archive-dev"
    assert len(config.schedule_windows) == 2
