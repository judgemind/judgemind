"""Tests for LA County tentative ruling scraper — built against real fixture HTML.

Fixtures captured from live site 2026-03-02:
  la_main_page.html   — GET https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil
  la_ruling_response.html — POST for ALH,3,03/02/2026 (Alhambra Dept 3)
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.la_tentatives import (
    CIVIL_URL,
    SELECT_NAME,
    LATentativeRulingsScraper,
    _extract_aspnet_tokens,
    _extract_ruling_fields,
    _parse_dropdown_options,
    _parse_option,
    default_config,
)
from framework import ContentFormat
from framework.models import CapturedDocument
from datetime import datetime

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _extract_aspnet_tokens — against real main page
# ---------------------------------------------------------------------------

def test_extract_tokens_finds_viewstate() -> None:
    html = _load("la_main_page.html")
    tokens = _extract_aspnet_tokens(html)
    assert tokens["__VIEWSTATE"]
    assert tokens["__VIEWSTATE"].startswith("/wE")  # ASP.NET base64 prefix


def test_extract_tokens_finds_all_three() -> None:
    html = _load("la_main_page.html")
    tokens = _extract_aspnet_tokens(html)
    assert "__VIEWSTATE" in tokens
    assert "__VIEWSTATEGENERATOR" in tokens
    assert "__EVENTVALIDATION" in tokens
    assert tokens["__VIEWSTATEGENERATOR"] == "65B48B29"


# ---------------------------------------------------------------------------
# _parse_dropdown_options — against real main page
# ---------------------------------------------------------------------------

def test_parse_dropdown_finds_97_options() -> None:
    html = _load("la_main_page.html")
    options = _parse_dropdown_options(html)
    assert len(options) == 97


def test_parse_dropdown_first_option() -> None:
    html = _load("la_main_page.html")
    options = _parse_dropdown_options(html)
    first = options[0]
    assert first.courthouse_code == "ALH"
    assert first.courthouse == "Alhambra Courthouse"
    assert first.department == "3"
    assert first.hearing_date == datetime(2026, 3, 2)
    assert first.value == "ALH,3,03/02/2026"


def test_parse_dropdown_stanley_mosk_present() -> None:
    html = _load("la_main_page.html")
    options = _parse_dropdown_options(html)
    mosk = [o for o in options if "Stanley Mosk" in o.courthouse]
    assert len(mosk) > 30  # Stanley Mosk dominates


def test_parse_dropdown_pomona_future_dates() -> None:
    html = _load("la_main_page.html")
    options = _parse_dropdown_options(html)
    pomona_h = [o for o in options if o.courthouse_code.strip() == "EA" and o.department == "H"]
    # Pomona South Dept H posts weeks out
    assert len(pomona_h) > 1
    dates = [o.hearing_date for o in pomona_h if o.hearing_date]
    assert max(dates) > datetime(2026, 3, 5)


# ---------------------------------------------------------------------------
# _parse_option — unit tests for value parsing
# ---------------------------------------------------------------------------

def test_parse_option_standard() -> None:
    opt = _parse_option("ALH,3,03/02/2026", "(Alhambra Courthouse:  Dept. 3) March 2, 2026")
    assert opt is not None
    assert opt.courthouse_code == "ALH"
    assert opt.department == "3"
    assert opt.hearing_date == datetime(2026, 3, 2)
    assert opt.courthouse == "Alhambra Courthouse"


def test_parse_option_with_space_in_code() -> None:
    # "BH ,205,03/02/2026" — courthouse code has trailing space
    opt = _parse_option("BH ,205,03/02/2026", "(Beverly Hills Courthouse:  Dept. 205) March 2, 2026")
    assert opt is not None
    assert opt.courthouse_code == "BH"
    assert opt.department == "205"


def test_parse_option_alphanumeric_dept() -> None:
    opt = _parse_option("CHA,F46,03/02/2026", "(Chatsworth Courthouse:  Dept. F46) March 2, 2026")
    assert opt is not None
    assert opt.department == "F46"


def test_parse_option_invalid_value_returns_none() -> None:
    opt = _parse_option("", "")
    assert opt is None


# ---------------------------------------------------------------------------
# _extract_ruling_fields — against real ruling response
# ---------------------------------------------------------------------------

def _make_ruling_doc() -> CapturedDocument:
    raw = _load("la_ruling_response.html").encode("utf-8")
    return CapturedDocument(
        scraper_id="ca-la-tentatives-civil",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 2, 18, 0, 0),
        content_format=ContentFormat.HTML,
        raw_content=raw,
        content_hash="",
        courthouse="Alhambra Courthouse",
        department="3",
        hearing_date=datetime(2026, 3, 2),
    )


def test_extract_fields_case_number() -> None:
    from bs4 import BeautifulSoup
    doc = _make_ruling_doc()
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    assert doc.case_number == "24NNCV02551"


def test_extract_fields_all_case_numbers() -> None:
    from bs4 import BeautifulSoup
    doc = _make_ruling_doc()
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    # Real fixture has 2 cases in this dept response
    assert "all_case_numbers" in doc.extra
    assert "26NNCP00062" in doc.extra["all_case_numbers"]


def test_extract_fields_judge_name() -> None:
    from bs4 import BeautifulSoup
    doc = _make_ruling_doc()
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    assert doc.judge_name is not None
    assert "Crowfoot" in doc.judge_name


def test_extract_fields_ruling_text_contains_tentative() -> None:
    from bs4 import BeautifulSoup
    doc = _make_ruling_doc()
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    assert doc.ruling_text is not None
    assert "GRANTED" in doc.ruling_text


def test_extract_fields_uses_speech_synthesis_div() -> None:
    """Verify we're extracting from div#speechSynthesis, not the whole page."""
    from bs4 import BeautifulSoup
    doc = _make_ruling_doc()
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    # Navigation text should not appear in ruling_text
    assert "Online Services" not in (doc.ruling_text or "")


# ---------------------------------------------------------------------------
# Full scraper run — mocked HTTP using real fixture content
# ---------------------------------------------------------------------------

@respx.mock
def test_full_run_with_real_fixtures() -> None:
    main_html = _load("la_main_page.html")
    ruling_html = _load("la_ruling_response.html")

    respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))
    respx.post(CIVIL_URL).mock(return_value=httpx.Response(200, text=ruling_html))

    config = default_config()
    config.request_delay_seconds = 0
    scraper = LATentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 97  # real fixture has 97 options


@respx.mock
def test_run_handles_get_failure() -> None:
    respx.get(CIVIL_URL).mock(return_value=httpx.Response(503))

    config = default_config()
    config.max_retries = 1
    config.request_delay_seconds = 0
    scraper = LATentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0


@respx.mock
def test_run_continues_when_single_post_fails() -> None:
    main_html = _load("la_main_page.html")
    ruling_html = _load("la_ruling_response.html")

    respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))

    call_count = 0

    def post_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(500)
        return httpx.Response(200, text=ruling_html)

    respx.post(CIVIL_URL).mock(side_effect=post_side_effect)

    config = default_config()
    config.request_delay_seconds = 0
    config.max_retries = 1
    scraper = LATentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 96  # 97 options minus 1 failed


def test_default_config() -> None:
    config = default_config(s3_bucket="judgemind-document-archive-dev")
    assert config.scraper_id == "ca-la-tentatives-civil"
    assert config.state == "CA"
    assert config.county == "Los Angeles"
    assert config.s3_bucket == "judgemind-document-archive-dev"
    assert len(config.schedule_windows) == 2
