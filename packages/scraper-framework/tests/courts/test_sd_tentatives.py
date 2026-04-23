"""Tests for San Diego County Odyssey ROA tentative rulings scraper (Phase 2).

Fixtures are realistic HTML pages based on Tyler Odyssey ROA standard patterns.
Since the actual Odyssey portal is protected by Cloudflare, fixtures represent
what the ROA pages look like AFTER the challenge has been solved.

Fixture files:
  sd_roa_case_detail.html       — Full ROA page with tentative ruling (Motion to Compel)
  sd_roa_demurrer.html          — ROA page with demurrer ruling (SUSTAINED/OVERRULED)
  sd_roa_no_ruling.html         — ROA page with no tentative ruling
  sd_roa_search_results.html    — SmartSearch results page with case link
  sd_roa_cloudflare_challenge.html — Cloudflare challenge page
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from courts.ca.sd_tentatives import (
    _SD_OUTCOME_MAP,
    PORTAL_BASE_URL,
    SDTentativeRulingsScraper,
    _apply_stealth,
    _sd_llm_enabled,
    _sd_llm_extract,
    default_config,
    has_cf_clearance,
    is_cloudflare_challenge,
    parse_case_details,
    parse_case_header,
    parse_motion_type,
    parse_outcome,
    parse_parties,
    parse_roa_page,
    parse_search_results,
    parse_tentative_ruling,
)
from framework.models import CapturedDocument, ContentFormat, ScraperConfig

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _make_config() -> ScraperConfig:
    return ScraperConfig(
        scraper_id="ca-sd-tentatives-test",
        state="CA",
        county="San Diego",
        court="Superior Court",
        target_urls=[PORTAL_BASE_URL],
        request_delay_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# is_cloudflare_challenge
# ---------------------------------------------------------------------------


class TestIsCloudflareChallenge:
    """Tests for Cloudflare challenge detection."""

    def test_detects_challenge_page(self) -> None:
        html = _load_html("sd_roa_cloudflare_challenge.html")
        assert is_cloudflare_challenge(html) is True

    def test_normal_page_not_challenge(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        assert is_cloudflare_challenge(html) is False

    def test_search_results_not_challenge(self) -> None:
        html = _load_html("sd_roa_search_results.html")
        assert is_cloudflare_challenge(html) is False

    def test_empty_page_not_challenge(self) -> None:
        assert is_cloudflare_challenge("") is False

    def test_detects_cf_chl_opt(self) -> None:
        assert is_cloudflare_challenge("<script>window._cf_chl_opt={}</script>") is True

    def test_detects_challenge_platform(self) -> None:
        html = '<script src="/cdn-cgi/challenge-platform/"></script>'
        assert is_cloudflare_challenge(html) is True


# ---------------------------------------------------------------------------
# has_cf_clearance — cookie detection
# ---------------------------------------------------------------------------


class TestHasCfClearance:
    """Tests for cf_clearance cookie detection."""

    @pytest.mark.asyncio
    async def test_returns_true_when_cookie_present(self) -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(
            return_value=[
                {"name": "__cf_bm", "value": "xyz"},
                {"name": "cf_clearance", "value": "abc123"},
            ]
        )
        assert await has_cf_clearance(context) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_cookie_absent(self) -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(return_value=[{"name": "__cf_bm", "value": "xyz"}])
        assert await has_cf_clearance(context) is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_cookies(self) -> None:
        context = AsyncMock()
        context.cookies = AsyncMock(return_value=[])
        assert await has_cf_clearance(context) is False


# ---------------------------------------------------------------------------
# _apply_stealth — stealth evasion application
# ---------------------------------------------------------------------------


class TestApplyStealth:
    """Tests for stealth evasion application."""

    @pytest.mark.asyncio
    async def test_applies_stealth_when_available(self) -> None:
        """Stealth().apply_stealth_async should be called when installed."""
        page = AsyncMock()
        mock_stealth_instance = MagicMock()
        mock_stealth_instance.apply_stealth_async = AsyncMock()
        mock_stealth_cls = MagicMock(return_value=mock_stealth_instance)

        with patch("playwright_stealth.Stealth", mock_stealth_cls):
            await _apply_stealth(page)

        mock_stealth_cls.assert_called_once()
        mock_stealth_instance.apply_stealth_async.assert_awaited_once_with(page)

    @pytest.mark.asyncio
    async def test_fallback_when_stealth_not_installed(self) -> None:
        """Falls back to minimal webdriver override when stealth unavailable."""
        page = AsyncMock()
        with patch.dict("sys.modules", {"playwright_stealth": None}):
            await _apply_stealth(page)
        page.evaluate.assert_awaited_once()


# ---------------------------------------------------------------------------
# parse_case_header — case number and title extraction
# ---------------------------------------------------------------------------


class TestParseCaseHeader:
    """Tests for case header parsing."""

    def test_extracts_case_number(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        case_number, _ = parse_case_header(soup)
        assert case_number == "24CU016153C"

    def test_extracts_case_title(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        _, case_title = parse_case_header(soup)
        assert case_title == "Aasi et al vs American Honda Motor Co Inc"

    def test_demurrer_case_number(self) -> None:
        html = _load_html("sd_roa_demurrer.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        case_number, _ = parse_case_header(soup)
        assert case_number == "25CU003887C"

    def test_demurrer_case_title(self) -> None:
        html = _load_html("sd_roa_demurrer.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        _, case_title = parse_case_header(soup)
        assert case_title == "Garcia vs Pacific Property Management LLC et al"

    def test_no_ruling_case(self) -> None:
        html = _load_html("sd_roa_no_ruling.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        case_number, case_title = parse_case_header(soup)
        assert case_number == "23CU005421C"
        assert case_title == "Thompson vs Johnson"


# ---------------------------------------------------------------------------
# parse_case_details — judge and department extraction
# ---------------------------------------------------------------------------


class TestParseCaseDetails:
    """Tests for case details (judge, department) parsing."""

    def test_extracts_judge_name(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        judge_name, _ = parse_case_details(soup)
        assert judge_name == "Matthew C. Braner"

    def test_extracts_department(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        _, department = parse_case_details(soup)
        assert department == "C-60"

    def test_demurrer_judge(self) -> None:
        html = _load_html("sd_roa_demurrer.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        judge_name, department = parse_case_details(soup)
        assert judge_name == "Karen S. Hewitt"
        assert department == "C-65"

    def test_no_ruling_case_details(self) -> None:
        html = _load_html("sd_roa_no_ruling.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        judge_name, department = parse_case_details(soup)
        assert judge_name == "Timothy R. Walsh"
        assert department == "N-28"


# ---------------------------------------------------------------------------
# parse_parties
# ---------------------------------------------------------------------------


class TestParseParties:
    """Tests for party information extraction."""

    def test_extracts_parties(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        parties = parse_parties(soup)
        assert len(parties) == 3

    def test_party_roles(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        parties = parse_parties(soup)
        roles = {p["role"] for p in parties}
        assert "plaintiff" in roles
        assert "defendant" in roles

    def test_party_names(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        parties = parse_parties(soup)
        names = {p["name"] for p in parties}
        assert "Sumayya Aasi" in names
        assert "Mohammad Aasi" in names
        assert "American Honda Motor Co Inc" in names

    def test_demurrer_parties(self) -> None:
        html = _load_html("sd_roa_demurrer.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        parties = parse_parties(soup)
        assert len(parties) == 3
        names = {p["name"] for p in parties}
        assert "Maria Garcia" in names
        assert "Pacific Property Management LLC" in names
        assert "John Doe" in names

    def test_deduplicates_parties(self) -> None:
        """Parties should be deduplicated by name."""
        html = _load_html("sd_roa_case_detail.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        parties = parse_parties(soup)
        names = [p["name"] for p in parties]
        assert len(names) == len(set(n.lower() for n in names))


# ---------------------------------------------------------------------------
# parse_tentative_ruling
# ---------------------------------------------------------------------------


class TestParseTentativeRuling:
    """Tests for tentative ruling extraction from the ROA events table."""

    def test_extracts_ruling_text(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        ruling_text, _ = parse_tentative_ruling(soup)
        assert ruling_text is not None
        assert "Motion to Compel Further Responses" in ruling_text
        assert "GRANTED IN PART AND DENIED IN PART" in ruling_text

    def test_extracts_hearing_date(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        _, hearing_date = parse_tentative_ruling(soup)
        assert hearing_date == datetime(2026, 3, 13)

    def test_demurrer_ruling_text(self) -> None:
        html = _load_html("sd_roa_demurrer.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        ruling_text, hearing_date = parse_tentative_ruling(soup)
        assert ruling_text is not None
        assert "OVERRULED" in ruling_text
        assert "SUSTAINED WITH LEAVE TO AMEND" in ruling_text
        assert hearing_date == datetime(2026, 3, 13)

    def test_no_ruling_returns_none(self) -> None:
        html = _load_html("sd_roa_no_ruling.html")
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        ruling_text, hearing_date = parse_tentative_ruling(soup)
        assert ruling_text is None
        assert hearing_date is None


# ---------------------------------------------------------------------------
# parse_outcome — unit tests
# ---------------------------------------------------------------------------


class TestParseOutcome:
    """Tests for outcome extraction from ruling text."""

    def test_granted(self) -> None:
        assert parse_outcome("The motion is GRANTED.") == "GRANTED"

    def test_denied(self) -> None:
        assert parse_outcome("The motion is DENIED.") == "DENIED"

    def test_granted_in_part(self) -> None:
        result = parse_outcome("The motion is GRANTED IN PART AND DENIED IN PART.")
        assert result == "GRANTED IN PART AND DENIED IN PART"

    def test_sustained(self) -> None:
        assert parse_outcome("The demurrer is SUSTAINED.") == "SUSTAINED"

    def test_sustained_with_leave(self) -> None:
        result = parse_outcome("The demurrer is SUSTAINED WITH LEAVE TO AMEND.")
        assert result == "SUSTAINED WITH LEAVE TO AMEND"

    def test_overruled(self) -> None:
        assert parse_outcome("The demurrer is OVERRULED.") == "OVERRULED"

    def test_moot(self) -> None:
        assert parse_outcome("The motion is MOOT.") == "MOOT"

    def test_off_calendar(self) -> None:
        assert parse_outcome("The matter is OFF CALENDAR.") == "OFF CALENDAR"

    def test_no_outcome(self) -> None:
        assert parse_outcome("The court considered the matter.") is None

    def test_from_fixture_case_detail(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        info = parse_roa_page(html)
        assert info.outcome == "GRANTED IN PART AND DENIED IN PART"

    def test_from_fixture_demurrer(self) -> None:
        html = _load_html("sd_roa_demurrer.html")
        info = parse_roa_page(html)
        # The first outcome in the ruling text is OVERRULED
        assert info.outcome == "OVERRULED"


# ---------------------------------------------------------------------------
# parse_motion_type — unit tests
# ---------------------------------------------------------------------------


class TestParseMotionType:
    """Tests for motion type extraction from ruling text."""

    def test_motion_to_compel(self) -> None:
        result = parse_motion_type("Motion to Compel Further Responses filed by plaintiff.")
        assert result == "Motion to Compel Further Responses"

    def test_demurrer(self) -> None:
        result = parse_motion_type("Demurrer to Complaint filed by defendant.")
        assert result == "Demurrer to Complaint"

    def test_summary_judgment(self) -> None:
        result = parse_motion_type("Motion for Summary Judgment filed by defendant.")
        assert result == "Motion for Summary Judgment"

    def test_motion_to_strike(self) -> None:
        result = parse_motion_type("Motion to Strike portions of the complaint.")
        assert result == "Motion to Strike"

    def test_motion_to_quash(self) -> None:
        result = parse_motion_type("Motion to Quash service of summons.")
        assert result == "Motion to Quash"

    def test_motion_for_sanctions(self) -> None:
        result = parse_motion_type("Motion for Sanctions against plaintiff.")
        assert result == "Motion for Sanctions"

    def test_preliminary_injunction(self) -> None:
        result = parse_motion_type("Hearing on Preliminary Injunction.")
        assert result == "Preliminary Injunction"

    def test_no_motion_type(self) -> None:
        assert parse_motion_type("The court heard argument on the matter.") is None

    def test_from_fixture_case_detail(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        info = parse_roa_page(html)
        assert info.motion_type == "Motion to Compel Further Responses"

    def test_from_fixture_demurrer(self) -> None:
        html = _load_html("sd_roa_demurrer.html")
        info = parse_roa_page(html)
        assert info.motion_type == "Demurrer to Complaint"


# ---------------------------------------------------------------------------
# parse_roa_page — full page parsing
# ---------------------------------------------------------------------------


class TestParseROAPage:
    """Tests for full ROA page parsing."""

    def test_case_detail_all_fields(self) -> None:
        html = _load_html("sd_roa_case_detail.html")
        info = parse_roa_page(html)

        assert info.case_number == "24CU016153C"
        assert info.case_title == "Aasi et al vs American Honda Motor Co Inc"
        assert info.judge_name == "Matthew C. Braner"
        assert info.department == "C-60"
        assert info.hearing_date == datetime(2026, 3, 13)
        assert info.has_tentative_ruling is True
        assert info.ruling_text is not None
        assert info.outcome == "GRANTED IN PART AND DENIED IN PART"
        assert info.motion_type == "Motion to Compel Further Responses"
        assert len(info.parties) == 3

    def test_demurrer_all_fields(self) -> None:
        html = _load_html("sd_roa_demurrer.html")
        info = parse_roa_page(html)

        assert info.case_number == "25CU003887C"
        assert info.case_title == "Garcia vs Pacific Property Management LLC et al"
        assert info.judge_name == "Karen S. Hewitt"
        assert info.department == "C-65"
        assert info.hearing_date == datetime(2026, 3, 13)
        assert info.has_tentative_ruling is True
        assert info.ruling_text is not None
        assert info.outcome == "OVERRULED"
        assert info.motion_type == "Demurrer to Complaint"
        assert len(info.parties) == 3

    def test_no_ruling_page(self) -> None:
        html = _load_html("sd_roa_no_ruling.html")
        info = parse_roa_page(html)

        assert info.case_number == "23CU005421C"
        assert info.case_title == "Thompson vs Johnson"
        assert info.judge_name == "Timothy R. Walsh"
        assert info.department == "N-28"
        assert info.has_tentative_ruling is False
        assert info.ruling_text is None
        assert info.outcome is None
        assert info.motion_type is None

    def test_cloudflare_page_no_crash(self) -> None:
        """Parsing a Cloudflare challenge page should not crash."""
        html = _load_html("sd_roa_cloudflare_challenge.html")
        info = parse_roa_page(html)
        assert info.has_tentative_ruling is False
        assert info.case_number is None


# ---------------------------------------------------------------------------
# parse_search_results
# ---------------------------------------------------------------------------


class TestParseSearchResults:
    """Tests for SmartSearch results parsing."""

    def test_extracts_case_link(self) -> None:
        html = _load_html("sd_roa_search_results.html")
        results = parse_search_results(html)
        assert len(results) == 1
        url, case_number = results[0]
        assert case_number == "24CU016153C"
        assert "/portal/Home/CaseDetail/12345678" in url

    def test_absolute_url(self) -> None:
        html = _load_html("sd_roa_search_results.html")
        results = parse_search_results(html)
        url, _ = results[0]
        assert url.startswith("https://")

    def test_empty_page(self) -> None:
        results = parse_search_results("<html><body></body></html>")
        assert results == []


# ---------------------------------------------------------------------------
# SDTentativeRulingsScraper — unit tests (no Playwright)
# ---------------------------------------------------------------------------


class TestSDTentativeRulingsScraper:
    """Tests for the scraper class (mocking Playwright)."""

    def test_no_case_numbers_returns_empty(self) -> None:
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=[])
        docs = scraper.fetch_documents()
        assert docs == []

    def test_none_case_numbers_returns_empty(self) -> None:
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=None)
        docs = scraper.fetch_documents()
        assert docs == []

    def test_parse_document_enriches_fields(self) -> None:
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config)

        html = _load_html("sd_roa_case_detail.html")
        doc = CapturedDocument(
            scraper_id="test",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=datetime(2026, 3, 12),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="abc123",
        )

        result = scraper.parse_document(doc)
        assert result.case_number == "24CU016153C"
        assert result.case_title == "Aasi et al vs American Honda Motor Co Inc"
        assert result.judge_name == "Matthew C. Braner"
        assert result.department == "C-60"
        assert result.hearing_date == datetime(2026, 3, 13)
        assert result.outcome == "GRANTED IN PART AND DENIED IN PART"
        assert result.motion_type == "Motion to Compel Further Responses"
        assert len(result.parties) == 3

    def test_parse_document_preserves_existing_fields(self) -> None:
        """parse_document should not overwrite already-populated fields."""
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config)

        html = _load_html("sd_roa_case_detail.html")
        doc = CapturedDocument(
            scraper_id="test",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=datetime(2026, 3, 12),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="abc123",
            case_number="EXISTING-CASE-NUMBER",
            judge_name="Existing Judge",
        )

        result = scraper.parse_document(doc)
        assert result.case_number == "EXISTING-CASE-NUMBER"
        assert result.judge_name == "Existing Judge"
        # Other fields should be populated from parsing
        assert result.case_title == "Aasi et al vs American Honda Motor Co Inc"
        assert result.department == "C-60"

    def test_parse_document_handles_invalid_html(self) -> None:
        """parse_document should not crash on malformed HTML."""
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config)

        doc = CapturedDocument(
            scraper_id="test",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=datetime(2026, 3, 12),
            content_format=ContentFormat.HTML,
            raw_content=b"<html><body>not a valid ROA page</body></html>",
            content_hash="abc123",
        )

        result = scraper.parse_document(doc)
        assert result.case_number is None


# ---------------------------------------------------------------------------
# default_config — factory test
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    """Tests for the default configuration factory."""

    def test_scraper_id(self) -> None:
        config = default_config()
        assert config.scraper_id == "ca-sd-tentatives"

    def test_state_and_county(self) -> None:
        config = default_config()
        assert config.state == "CA"
        assert config.county == "San Diego"

    def test_schedule_windows(self) -> None:
        config = default_config()
        assert len(config.schedule_windows) == 2
        # Primary window: 4:15 PM (after court's 4:00 PM posting deadline)
        assert config.schedule_windows[0].start.hour == 16
        assert config.schedule_windows[0].start.minute == 15

    def test_s3_bucket_parameter(self) -> None:
        config = default_config(s3_bucket="my-bucket")
        assert config.s3_bucket == "my-bucket"

    def test_poll_interval(self) -> None:
        config = default_config()
        assert config.poll_interval_seconds == 86400  # daily

    def test_request_delay(self) -> None:
        config = default_config()
        assert config.request_delay_seconds == 3.0  # 3 second delay for rate limiting


# ---------------------------------------------------------------------------
# Playwright-mocked async tests for fetch_documents / _fetch_all
# ---------------------------------------------------------------------------


def _make_mock_page(
    content_sequence: list[str],
    cf_clearance: bool = False,
) -> AsyncMock:
    """Create a mock Playwright page that returns content_sequence on successive
    content() calls.

    Parameters
    ----------
    content_sequence : list[str]
        HTML strings returned by successive ``page.content()`` calls.
    cf_clearance : bool
        If True, the mock context's ``cookies()`` returns a ``cf_clearance``
        cookie, simulating a solved Cloudflare challenge.
    """
    page = AsyncMock()
    page.goto = AsyncMock()
    page.content = AsyncMock(side_effect=content_sequence)
    page.wait_for_url = AsyncMock()
    page.evaluate = AsyncMock()

    # Set up context with cookies support for cf_clearance checks
    context = AsyncMock()
    if cf_clearance:
        context.cookies = AsyncMock(return_value=[{"name": "cf_clearance", "value": "abc123"}])
    else:
        context.cookies = AsyncMock(return_value=[])
    page.context = context

    return page


def _make_mock_browser(page: AsyncMock) -> AsyncMock:
    """Create a mock browser that yields the given page."""
    context = page.context

    context.new_page = AsyncMock(return_value=page)

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    return browser


class TestFetchDocumentsWithMockedPlaywright:
    """Integration tests with mocked Playwright browser."""

    def test_fetch_with_ruling_found(self) -> None:
        """Full flow: CF solved, search returns result, detail has ruling."""
        portal_html = "<html><body>Portal home</body></html>"
        search_html = _load_html("sd_roa_search_results.html")
        detail_html = _load_html("sd_roa_case_detail.html")

        page = _make_mock_page([portal_html, search_html, detail_html])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            docs = scraper.fetch_documents()

        assert len(docs) == 1
        doc = docs[0]
        assert doc.case_number == "24CU016153C"
        assert doc.judge_name == "Matthew C. Braner"
        assert doc.department == "C-60"
        assert doc.outcome == "GRANTED IN PART AND DENIED IN PART"
        assert doc.motion_type == "Motion to Compel Further Responses"
        assert len(doc.parties) == 3

    def test_fetch_no_ruling_on_roa(self) -> None:
        """Case found but ROA has no tentative ruling."""
        portal_html = "<html><body>Portal</body></html>"
        search_html = _load_html("sd_roa_search_results.html")
        detail_html = _load_html("sd_roa_no_ruling.html")

        page = _make_mock_page([portal_html, search_html, detail_html])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["23CU005421C"])

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            docs = scraper.fetch_documents()

        assert len(docs) == 0

    def test_fetch_no_search_results(self) -> None:
        """SmartSearch returns no results for the case number."""
        portal_html = "<html><body>Portal</body></html>"
        empty_search = "<html><body><div>No results</div></body></html>"

        page = _make_mock_page([portal_html, empty_search])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["99XX000000X"])

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            docs = scraper.fetch_documents()

        assert len(docs) == 0

    def test_fetch_cloudflare_challenge_blocks(self) -> None:
        """Cloudflare challenge is not solved after all retries — returns empty."""
        cf_html = _load_html("sd_roa_cloudflare_challenge.html")

        # Need enough cf_html entries for all retry attempts.
        # Each attempt: 1 content() after goto + 1 content() after poll timeout
        # = 2 per attempt, 3 attempts = 6 minimum.
        page = _make_mock_page([cf_html] * 10)
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])

        # Patch timeouts to very small values so retries complete quickly
        with (
            patch(
                "playwright.async_api.async_playwright",
                return_value=mock_pw_ctx,
            ),
            patch("courts.ca.sd_tentatives.CF_CHALLENGE_TIMEOUT", 0.1),
            patch("courts.ca.sd_tentatives.CF_POLL_INTERVAL", 0.05),
        ):
            docs = scraper.fetch_documents()

        assert len(docs) == 0

    def test_fetch_cloudflare_rechallenge_on_search(self) -> None:
        """CF challenge resolved initially but re-triggered on search."""
        portal_html = "<html><body>Portal</body></html>"
        cf_html = _load_html("sd_roa_cloudflare_challenge.html")

        page = _make_mock_page([portal_html, cf_html])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            docs = scraper.fetch_documents()

        assert len(docs) == 0

    def test_fetch_cloudflare_rechallenge_on_detail(self) -> None:
        """CF re-triggered on case detail page."""
        portal_html = "<html><body>Portal</body></html>"
        search_html = _load_html("sd_roa_search_results.html")
        cf_html = _load_html("sd_roa_cloudflare_challenge.html")

        page = _make_mock_page([portal_html, search_html, cf_html])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            docs = scraper.fetch_documents()

        assert len(docs) == 0

    def test_fetch_multiple_cases(self) -> None:
        """Multiple cases: one with ruling, one without."""
        portal_html = "<html><body>Portal</body></html>"
        search1 = _load_html("sd_roa_search_results.html")
        detail1 = _load_html("sd_roa_case_detail.html")
        # Second case: reuse search results but no ruling page
        search2 = _load_html("sd_roa_search_results.html")
        detail2 = _load_html("sd_roa_no_ruling.html")

        page = _make_mock_page([portal_html, search1, detail1, search2, detail2])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(
            config,
            case_numbers=["24CU016153C", "23CU005421C"],
        )

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            docs = scraper.fetch_documents()

        # Only the first case has a ruling
        assert len(docs) == 1
        assert docs[0].case_number == "24CU016153C"

    def test_fetch_with_proxy(self) -> None:
        """Proxy URL is passed to Playwright launch kwargs."""
        portal_html = "<html><body>Portal</body></html>"
        search_html = _load_html("sd_roa_search_results.html")
        detail_html = _load_html("sd_roa_case_detail.html")

        page = _make_mock_page([portal_html, search_html, detail_html])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(
            config,
            case_numbers=["24CU016153C"],
            proxy_url="http://proxy:8080",
        )

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            docs = scraper.fetch_documents()

        # Verify proxy was passed
        launch_call = mock_pw.chromium.launch.call_args
        assert launch_call.kwargs.get("proxy") == {"server": "http://proxy:8080"}
        assert len(docs) == 1

    def test_fetch_case_exception_continues(self) -> None:
        """Exception on one case should not stop other cases."""
        portal_html = "<html><body>Portal</body></html>"
        search_html = _load_html("sd_roa_search_results.html")
        detail_html = _load_html("sd_roa_case_detail.html")

        # First search raises, second succeeds
        page = AsyncMock()
        page.wait_for_url = AsyncMock()
        call_count = 0

        async def mock_goto(url: str, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1

        page.goto = AsyncMock(side_effect=mock_goto)

        content_calls = [portal_html]

        async def mock_content() -> str:
            return content_calls.pop(0)

        # First case: goto raises on search
        page.goto = AsyncMock(
            side_effect=[
                None,  # portal goto
                RuntimeError("Network error"),  # search goto fails
                None,  # search goto succeeds
                None,  # detail goto
            ]
        )
        page.content = AsyncMock(
            side_effect=[
                portal_html,  # portal content
                search_html,  # search content
                detail_html,  # detail content
            ]
        )
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(
            config,
            case_numbers=["FAIL-CASE", "24CU016153C"],
        )

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            docs = scraper.fetch_documents()

        # First case failed, second succeeded
        assert len(docs) == 1
        assert docs[0].case_number == "24CU016153C"

    def test_fetch_browser_close_on_exception(self) -> None:
        """Browser is closed even if an exception occurs."""
        portal_html = "<html><body>Portal</body></html>"

        page = AsyncMock()
        page.goto = AsyncMock(side_effect=RuntimeError("Fatal error"))
        page.content = AsyncMock(return_value=portal_html)
        page.wait_for_url = AsyncMock()

        browser = _make_mock_browser(page)
        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            # Should not raise — error handled gracefully
            scraper.fetch_documents()

        # Browser should have been closed
        browser.close.assert_awaited_once()

    def test_cloudflare_solved_via_cookie(self) -> None:
        """CF challenge detected, solved when cf_clearance cookie appears."""
        cf_html = _load_html("sd_roa_cloudflare_challenge.html")
        search_html = _load_html("sd_roa_search_results.html")
        detail_html = _load_html("sd_roa_case_detail.html")

        # cf_clearance=True simulates the cookie appearing after challenge
        page = _make_mock_page(
            [cf_html, search_html, detail_html],
            cf_clearance=True,
        )
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            docs = scraper.fetch_documents()

        assert len(docs) == 1

    def test_cloudflare_retry_succeeds_on_second_attempt(self) -> None:
        """Challenge fails on first attempt, succeeds on second with cookie."""
        cf_html = _load_html("sd_roa_cloudflare_challenge.html")
        search_html = _load_html("sd_roa_search_results.html")
        detail_html = _load_html("sd_roa_case_detail.html")

        # Content sequence:
        # Attempt 1: cf_html (challenge detected after goto)
        #            cf_html (fallback check after poll timeout — still challenge)
        # Attempt 2: cf_html (challenge detected after goto)
        #            [cookie appears during polling — solved, no fallback needed]
        # Case fetch: search_html (search page), detail_html (case detail)
        page = _make_mock_page(
            [cf_html, cf_html, cf_html, search_html, detail_html],
        )

        # Track how many times cookies() is polled to simulate the cookie
        # appearing on the second retry attempt. With CF_CHALLENGE_TIMEOUT=0.1
        # and CF_POLL_INTERVAL=0.05, each attempt polls ~2 times.
        call_count = {"n": 0}

        async def cookies_side_effect() -> list[dict[str, str]]:
            call_count["n"] += 1
            # First attempt: polls 1-2, no cookie
            # Second attempt: cookie appears immediately on first poll
            if call_count["n"] >= 3:
                return [{"name": "cf_clearance", "value": "solved"}]
            return []

        page.context.cookies = AsyncMock(side_effect=cookies_side_effect)

        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])

        with (
            patch(
                "playwright.async_api.async_playwright",
                return_value=mock_pw_ctx,
            ),
            patch("courts.ca.sd_tentatives.CF_CHALLENGE_TIMEOUT", 0.1),
            patch("courts.ca.sd_tentatives.CF_POLL_INTERVAL", 0.05),
        ):
            docs = scraper.fetch_documents()

        assert len(docs) == 1

    def test_stealth_is_applied(self) -> None:
        """Verify that _apply_stealth is called during browser setup."""
        portal_html = "<html><body>Portal home</body></html>"
        search_html = _load_html("sd_roa_search_results.html")
        detail_html = _load_html("sd_roa_case_detail.html")

        page = _make_mock_page([portal_html, search_html, detail_html])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])

        with (
            patch(
                "playwright.async_api.async_playwright",
                return_value=mock_pw_ctx,
            ),
            patch(
                "courts.ca.sd_tentatives._apply_stealth",
                new_callable=AsyncMock,
            ) as mock_stealth,
        ):
            docs = scraper.fetch_documents()

        # Stealth should be applied to the page
        mock_stealth.assert_awaited_once_with(page)
        assert len(docs) == 1

    def test_session_reuse_single_cf_solve(self) -> None:
        """Single CF solve serves multiple case lookups without re-challenge."""
        portal_html = "<html><body>Portal home</body></html>"
        search1 = _load_html("sd_roa_search_results.html")
        detail1 = _load_html("sd_roa_case_detail.html")
        search2 = _load_html("sd_roa_search_results.html")
        detail2 = _load_html("sd_roa_case_detail.html")

        # Portal + 2 cases, each with search + detail (no re-challenge)
        page = _make_mock_page([portal_html, search1, detail1, search2, detail2])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(
            config,
            case_numbers=["24CU016153C", "24CU016154C"],
        )

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            docs = scraper.fetch_documents()

        # Both cases should be fetched using the same session
        assert len(docs) == 2

    def test_chromium_launch_args_include_stealth_flags(self) -> None:
        """Browser is launched with anti-detection flags."""
        portal_html = "<html><body>Portal</body></html>"
        page = _make_mock_page([portal_html])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["X"])

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            scraper.fetch_documents()

        launch_call = mock_pw.chromium.launch.call_args
        args = launch_call.kwargs.get("args", [])
        assert "--disable-blink-features=AutomationControlled" in args
        assert "--no-sandbox" in args
        assert "--disable-dev-shm-usage" in args

    def test_context_has_locale_and_timezone(self) -> None:
        """Browser context is created with locale and timezone for realism."""
        portal_html = "<html><body>Portal</body></html>"
        page = _make_mock_page([portal_html])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["X"])

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            scraper.fetch_documents()

        context_call = browser.new_context.call_args
        assert context_call.kwargs.get("locale") == "en-US"
        assert context_call.kwargs.get("timezone_id") == "America/Los_Angeles"

    def test_user_agent_is_recent_chrome(self) -> None:
        """User-Agent string uses a recent Chrome version (not 120)."""
        portal_html = "<html><body>Portal</body></html>"
        page = _make_mock_page([portal_html])
        browser = _make_mock_browser(page)

        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=browser)

        mock_pw_ctx = AsyncMock()
        mock_pw_ctx.__aenter__ = AsyncMock(return_value=mock_pw)
        mock_pw_ctx.__aexit__ = AsyncMock(return_value=False)

        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["X"])

        with patch(
            "playwright.async_api.async_playwright",
            return_value=mock_pw_ctx,
        ):
            scraper.fetch_documents()

        context_call = browser.new_context.call_args
        ua = context_call.kwargs.get("user_agent", "")
        assert "Chrome/131" in ua


# ---------------------------------------------------------------------------
# LLM extraction tests (#2056)
# ---------------------------------------------------------------------------


class TestSdLlmEnabled:
    """Feature flag tests for _sd_llm_enabled()."""

    def test_enabled_when_env_true(self) -> None:
        with patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "true"}):
            assert _sd_llm_enabled() is True

    def test_enabled_when_env_1(self) -> None:
        with patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "1"}):
            assert _sd_llm_enabled() is True

    def test_enabled_when_env_yes(self) -> None:
        with patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "yes"}):
            assert _sd_llm_enabled() is True

    def test_disabled_when_env_false(self) -> None:
        with patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "false"}):
            assert _sd_llm_enabled() is False

    def test_disabled_when_env_empty(self) -> None:
        with patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": ""}):
            assert _sd_llm_enabled() is False

    def test_disabled_when_env_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert _sd_llm_enabled() is False

    def test_case_insensitive(self) -> None:
        with patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "TRUE"}):
            assert _sd_llm_enabled() is True


class TestSdOutcomeMap:
    """Verify the _SD_OUTCOME_MAP covers all expected mappings."""

    def test_granted(self) -> None:
        assert _SD_OUTCOME_MAP["granted"] == "granted"

    def test_denied(self) -> None:
        assert _SD_OUTCOME_MAP["denied"] == "denied"

    def test_sustained_maps_to_granted(self) -> None:
        assert _SD_OUTCOME_MAP["sustained"] == "granted"

    def test_sustained_with_leave_maps_to_granted(self) -> None:
        assert _SD_OUTCOME_MAP["sustained_with_leave"] == "granted"

    def test_overruled_maps_to_denied(self) -> None:
        assert _SD_OUTCOME_MAP["overruled"] == "denied"

    def test_moot(self) -> None:
        assert _SD_OUTCOME_MAP["moot"] == "moot"

    def test_none_maps_to_none(self) -> None:
        assert _SD_OUTCOME_MAP[None] is None

    def test_granted_in_part(self) -> None:
        assert _SD_OUTCOME_MAP["granted_in_part"] == "granted_in_part"


@dataclass
class _FakeLLMResponse:
    """Minimal stand-in for an LLM provider response."""

    text: str
    input_tokens: int = 10
    output_tokens: int = 5


class TestSdLlmExtract:
    """Unit tests for _sd_llm_extract() with mocked call_llm."""

    def _patch_call_llm(self, response: _FakeLLMResponse | None) -> MagicMock:
        mock = MagicMock(return_value=response)
        return mock

    def test_success_granted(self) -> None:
        resp = _FakeLLMResponse(text='{"outcome": "granted", "motion_type": "Motion to Compel"}')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("The motion is GRANTED.")
        assert result is not None
        assert result["outcome"] == "granted"
        assert result["motion_type"] == "Motion to Compel"

    def test_sustained_maps_to_granted(self) -> None:
        resp = _FakeLLMResponse(text='{"outcome": "sustained", "motion_type": "Demurrer"}')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Demurrer is SUSTAINED.")
        assert result is not None
        assert result["outcome"] == "granted"

    def test_overruled_maps_to_denied(self) -> None:
        resp = _FakeLLMResponse(text='{"outcome": "overruled", "motion_type": "Demurrer"}')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Demurrer is OVERRULED.")
        assert result is not None
        assert result["outcome"] == "denied"

    def test_empty_ruling_text_returns_none(self) -> None:
        assert _sd_llm_extract("") is None
        assert _sd_llm_extract("   ") is None

    def test_null_response_returns_none(self) -> None:
        mock = self._patch_call_llm(None)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Some ruling text")
        assert result is None

    def test_invalid_json_returns_none(self) -> None:
        resp = _FakeLLMResponse(text="not json at all")
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Some ruling text")
        assert result is None

    def test_markdown_fencing_stripped(self) -> None:
        resp = _FakeLLMResponse(text='```json\n{"outcome": "denied", "motion_type": "MSJ"}\n```')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Summary judgment is denied.")
        assert result is not None
        assert result["outcome"] == "denied"

    def test_case_number_passed_as_metadata(self) -> None:
        resp = _FakeLLMResponse(text='{"outcome": "granted", "motion_type": "MTC"}')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            _sd_llm_extract("Ruling text", case_number="24CU016153C")
        call_args = mock.call_args
        assert "24CU016153C" in call_args.kwargs["user_message"]

    def test_unknown_outcome_maps_to_none(self) -> None:
        resp = _FakeLLMResponse(text='{"outcome": "withdrawn", "motion_type": "MTC"}')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Some text")
        assert result is not None
        assert result["outcome"] is None

    def test_null_outcome_in_response(self) -> None:
        resp = _FakeLLMResponse(text='{"outcome": null, "motion_type": "MTC"}')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Some text")
        assert result is not None
        assert result["outcome"] is None

    def test_non_dict_response_returns_none(self) -> None:
        resp = _FakeLLMResponse(text='["not", "a", "dict"]')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Some text")
        assert result is None

    def test_list_outcome_does_not_crash(self) -> None:
        """LLM returns a list for outcome — should not raise TypeError."""
        resp = _FakeLLMResponse(text='{"outcome": ["granted"], "motion_type": "MTC"}')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Some text")
        assert result is not None
        assert result["outcome"] is None  # unhashable type maps to None

    def test_dict_outcome_does_not_crash(self) -> None:
        """LLM returns a dict for outcome — should not raise TypeError."""
        resp = _FakeLLMResponse(text='{"outcome": {"value": "granted"}, "motion_type": "MTC"}')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Some text")
        assert result is not None
        assert result["outcome"] is None

    def test_int_outcome_does_not_crash(self) -> None:
        """LLM returns an int for outcome — should not raise TypeError."""
        resp = _FakeLLMResponse(text='{"outcome": 42, "motion_type": "MTC"}')
        mock = self._patch_call_llm(resp)
        with patch("ingestion.llm_providers.call_llm", mock):
            result = _sd_llm_extract("Some text")
        assert result is not None
        assert result["outcome"] is None


class TestParseDocumentWithLLM:
    """Integration tests for parse_document with LLM extraction enabled/disabled."""

    def _make_doc(self, fixture: str = "sd_roa_case_detail.html") -> CapturedDocument:
        html = _load_html(fixture)
        return CapturedDocument(
            scraper_id="test",
            source_url="https://odyroa.sdcourt.ca.gov/portal/Home/CaseDetail/123",
            raw_content=html.encode("utf-8"),
            content_format=ContentFormat.HTML,
            state="CA",
            county="San Diego",
            court="Superior Court",
            capture_timestamp=datetime(2026, 3, 12),
            content_hash="abc123",
            extra={},
        )

    def test_disabled_uses_regex(self) -> None:
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])
        doc = self._make_doc()
        with patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "false"}):
            result = scraper.parse_document(doc)
        assert result.outcome is not None
        assert "_llm_extracted" not in result.extra

    def test_enabled_uses_llm(self) -> None:
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])
        doc = self._make_doc()
        resp = _FakeLLMResponse(
            text='{"outcome": "granted", "motion_type": "Motion to Compel Further Responses"}'
        )
        mock_llm = MagicMock(return_value=resp)
        with (
            patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "true"}),
            patch("ingestion.llm_providers.call_llm", mock_llm),
        ):
            result = scraper.parse_document(doc)
        assert result.outcome == "granted"
        assert result.motion_type == "Motion to Compel Further Responses"
        assert result.extra.get("_llm_extracted") is True

    def test_demurrer_mapping(self) -> None:
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["37-2024-00060876"])
        doc = self._make_doc("sd_roa_demurrer.html")
        resp = _FakeLLMResponse(
            text='{"outcome": "sustained_with_leave", "motion_type": "Demurrer"}'
        )
        mock_llm = MagicMock(return_value=resp)
        with (
            patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "true"}),
            patch("ingestion.llm_providers.call_llm", mock_llm),
        ):
            result = scraper.parse_document(doc)
        assert result.outcome == "granted"

    def test_llm_failure_falls_back_to_regex(self) -> None:
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])
        doc = self._make_doc()
        mock_llm = MagicMock(return_value=None)
        with (
            patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "true"}),
            patch("ingestion.llm_providers.call_llm", mock_llm),
        ):
            result = scraper.parse_document(doc)
        assert result.outcome is not None
        assert "_llm_extracted" not in result.extra

    def test_partial_llm_result_falls_back_for_missing_field(self) -> None:
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])
        doc = self._make_doc()
        resp = _FakeLLMResponse(text='{"outcome": "granted", "motion_type": null}')
        mock_llm = MagicMock(return_value=resp)
        with (
            patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "true"}),
            patch("ingestion.llm_providers.call_llm", mock_llm),
        ):
            result = scraper.parse_document(doc)
        assert result.outcome == "granted"
        assert result.motion_type is not None

    def test_no_ruling_text_skips_llm(self) -> None:
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])
        doc = self._make_doc("sd_roa_no_ruling.html")
        mock_llm = MagicMock()
        with (
            patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "true"}),
            patch("ingestion.llm_providers.call_llm", mock_llm),
        ):
            scraper.parse_document(doc)
        mock_llm.assert_not_called()

    def test_other_fields_still_from_html(self) -> None:
        config = _make_config()
        scraper = SDTentativeRulingsScraper(config, case_numbers=["24CU016153C"])
        doc = self._make_doc()
        resp = _FakeLLMResponse(text='{"outcome": "granted", "motion_type": "MTC"}')
        mock_llm = MagicMock(return_value=resp)
        with (
            patch.dict("os.environ", {"ENABLE_SD_LLM_EXTRACTION": "true"}),
            patch("ingestion.llm_providers.call_llm", mock_llm),
        ):
            result = scraper.parse_document(doc)
        assert result.case_number is not None
        assert result.judge_name is not None
        assert result.ruling_text is not None


class TestSdExtractionConfig:
    """Verify San Diego extraction config is registered."""

    def test_sd_config_registered(self) -> None:
        from framework.extraction_config import get_county_extraction_config

        cfg = get_county_extraction_config("CA", "SAN DIEGO")
        assert cfg is not None

    def test_sd_config_method_is_llm(self) -> None:
        from framework.extraction_config import ExtractionMethod, get_county_extraction_config

        cfg = get_county_extraction_config("CA", "SAN DIEGO")
        assert cfg is not None
        assert cfg.method == ExtractionMethod.LLM

    def test_sd_config_provider_is_google(self) -> None:
        from framework.extraction_config import get_county_extraction_config

        cfg = get_county_extraction_config("CA", "SAN DIEGO")
        assert cfg is not None
        assert cfg.provider == "google"

    def test_sd_config_has_system_prompt(self) -> None:
        from framework.extraction_config import get_county_extraction_config

        cfg = get_county_extraction_config("CA", "SAN DIEGO")
        assert cfg is not None
        assert cfg.system_prompt is not None
        assert len(cfg.system_prompt) > 100

    def test_sd_config_case_insensitive(self) -> None:
        from framework.extraction_config import get_county_extraction_config

        cfg = get_county_extraction_config("ca", "san diego")
        assert cfg is not None
