"""Tests for the LA County tentative rulings scraper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import respx
import httpx

from courts.ca.la_tentatives import (
    LATentativeRulingsScraper,
    _extract_aspnet_tokens,
    _parse_dropdown_options,
    _parse_option_text,
    _extract_ruling_fields,
    default_config,
    CIVIL_URL,
)
from framework import CapturedDocument, ContentFormat

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# Unit tests — pure parsing functions
# ---------------------------------------------------------------------------


def test_extract_aspnet_tokens() -> None:
    html = _load_fixture("la_main_page.html")
    tokens = _extract_aspnet_tokens(html)
    assert tokens["__VIEWSTATE"] == "abc123viewstate"
    assert tokens["__VIEWSTATEGENERATOR"] == "gen456"
    assert tokens["__EVENTVALIDATION"] == "ev789validation"


def test_parse_dropdown_options() -> None:
    html = _load_fixture("la_main_page.html")
    options = _parse_dropdown_options(html)
    assert len(options) == 4
    assert options[0].courthouse == "Stanley Mosk"
    assert options[0].department == "52"
    assert options[0].hearing_date == datetime(2026, 3, 3)
    assert options[1].courthouse == "Santa Monica"
    assert options[1].department == "M"


def test_parse_dropdown_options_skips_placeholder() -> None:
    html = _load_fixture("la_main_page.html")
    options = _parse_dropdown_options(html)
    # The "-- Select --" option (value="0") should be excluded
    assert all(opt.value != "0" for opt in options)


def test_parse_option_text_standard_format() -> None:
    opt = _parse_option_text("1", "(Stanley Mosk: Dept. 52) March 3, 2026")
    assert opt is not None
    assert opt.courthouse == "Stanley Mosk"
    assert opt.department == "52"
    assert opt.hearing_date == datetime(2026, 3, 3)


def test_parse_option_text_future_date() -> None:
    opt = _parse_option_text("3", "(Pomona South: Dept. H) March 12, 2026")
    assert opt is not None
    assert opt.courthouse == "Pomona South"
    assert opt.department == "H"
    assert opt.hearing_date == datetime(2026, 3, 12)


def test_parse_option_text_unparseable_returns_unknown() -> None:
    opt = _parse_option_text("99", "Garbled text with no matching pattern")
    assert opt is not None
    assert opt.courthouse == "Unknown"
    assert opt.department == "Unknown"
    assert opt.hearing_date is None


def test_extract_ruling_fields_case_number() -> None:
    from bs4 import BeautifulSoup

    html = _load_fixture("la_ruling_response.html")
    soup = BeautifulSoup(html, "lxml")
    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime.utcnow(),
        content_format=ContentFormat.HTML,
        raw_content=html.encode(),
        content_hash="",
    )
    _extract_ruling_fields(soup, doc)
    assert doc.case_number == "25STCV13276"


def test_extract_ruling_fields_judge_name() -> None:
    from bs4 import BeautifulSoup

    html = _load_fixture("la_ruling_response.html")
    soup = BeautifulSoup(html, "lxml")
    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime.utcnow(),
        content_format=ContentFormat.HTML,
        raw_content=html.encode(),
        content_hash="",
    )
    _extract_ruling_fields(soup, doc)
    assert doc.judge_name is not None
    assert "Smith" in doc.judge_name


def test_extract_ruling_fields_ruling_text_populated() -> None:
    from bs4 import BeautifulSoup

    html = _load_fixture("la_ruling_response.html")
    soup = BeautifulSoup(html, "lxml")
    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime.utcnow(),
        content_format=ContentFormat.HTML,
        raw_content=html.encode(),
        content_hash="",
    )
    _extract_ruling_fields(soup, doc)
    assert doc.ruling_text is not None
    assert "GRANTED" in doc.ruling_text


# ---------------------------------------------------------------------------
# Integration-style tests — full scraper run with mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_full_scraper_run() -> None:
    """Full run against mocked HTTP — verifies the end-to-end fetch→parse flow."""
    main_html = _load_fixture("la_main_page.html")
    ruling_html = _load_fixture("la_ruling_response.html")

    # Mock GET for main page
    respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))
    # Mock all POSTs with the ruling response
    respx.post(CIVIL_URL).mock(return_value=httpx.Response(200, text=ruling_html))

    config = default_config()
    config.request_delay_seconds = 0  # no delay in tests
    scraper = LATentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 4  # 4 dropdown options


@respx.mock
def test_scraper_handles_get_failure() -> None:
    respx.get(CIVIL_URL).mock(return_value=httpx.Response(503))

    config = default_config()
    config.max_retries = 1
    config.request_delay_seconds = 0
    scraper = LATentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is False
    assert health.records_captured == 0


@respx.mock
def test_scraper_continues_when_single_post_fails() -> None:
    """If one POST fails, the rest of the options should still be captured."""
    main_html = _load_fixture("la_main_page.html")
    ruling_html = _load_fixture("la_ruling_response.html")

    respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))

    call_count = 0

    def post_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return httpx.Response(500)
        return httpx.Response(200, text=ruling_html)

    respx.post(CIVIL_URL).mock(side_effect=post_side_effect)

    config = default_config()
    config.request_delay_seconds = 0
    config.max_retries = 1
    scraper = LATentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 3  # 4 options minus 1 failed


def test_default_config_structure() -> None:
    config = default_config(s3_bucket="my-bucket")
    assert config.scraper_id == "ca-la-tentatives-civil"
    assert config.state == "CA"
    assert config.county == "Los Angeles"
    assert config.s3_bucket == "my-bucket"
    assert len(config.schedule_windows) == 2
