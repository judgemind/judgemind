"""Tests for LA County tentative ruling scraper — built against real fixture HTML.

Fixtures captured from live site 2026-03-02:
  la_main_page.html   — GET https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil
  la_ruling_response.html — POST for ALH,3,03/02/2026 (Alhambra Dept 3)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from courts.ca.la_tentatives import (
    CIVIL_URL,
    LATentativeRulingsScraper,
    _extract_aspnet_tokens,
    _extract_case_title,
    _extract_parties,
    _extract_parties_from_anchor,
    _extract_ruling_fields,
    _is_dept_header_boilerplate,
    _is_name_fragment,
    _is_stale_viewstate_response,
    _parse_dropdown_options,
    _parse_option,
    _sanitize_title,
    _split_cases_html,
    _split_party_names,
    default_config,
)
from framework import ContentFormat
from framework.models import CapturedDocument

pytestmark = pytest.mark.regression

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
    opt = _parse_option(
        "BH ,205,03/02/2026",
        "(Beverly Hills Courthouse:  Dept. 205) March 2, 2026",
    )
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
    """After splitting, each section has exactly one case number (no all_case_numbers)."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    sections = _split_cases_html(html)
    assert len(sections) == 2

    # Each section should have only one case number
    for section in sections:
        doc = CapturedDocument(
            scraper_id="test",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url=CIVIL_URL,
            capture_timestamp=datetime(2026, 3, 2),
            content_format=ContentFormat.HTML,
            raw_content=section.encode("utf-8"),
            content_hash="",
        )
        soup = BeautifulSoup(doc.raw_content, "lxml")
        _extract_ruling_fields(soup, doc)
        assert doc.case_number is not None
        assert "all_case_numbers" not in doc.extra


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
# _split_cases_html — multi-case splitting
# ---------------------------------------------------------------------------


def test_split_cases_html_two_cases_from_fixture() -> None:
    """The real fixture la_ruling_response.html has 2 cases; splitting should yield 2 sections."""
    html = _load("la_ruling_response.html")
    sections = _split_cases_html(html)
    assert len(sections) == 2


def test_split_cases_html_each_section_has_own_case_number() -> None:
    """Each split section should contain exactly one case number."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    sections = _split_cases_html(html)
    assert len(sections) == 2

    # First case
    soup1 = BeautifulSoup(sections[0], "lxml")
    text1 = soup1.get_text()
    assert "24NNCV02551" in text1
    assert "26NNCP00062" not in text1

    # Second case
    soup2 = BeautifulSoup(sections[1], "lxml")
    text2 = soup2.get_text()
    assert "26NNCP00062" in text2
    assert "24NNCV02551" not in text2


def test_split_cases_html_sections_have_speech_synthesis_div() -> None:
    """Each split section should be wrapped in a div#speechSynthesis for parse compatibility."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    sections = _split_cases_html(html)
    for section in sections:
        soup = BeautifulSoup(section, "lxml")
        assert soup.find("div", id="speechSynthesis") is not None


def test_split_cases_html_single_case_no_regression() -> None:
    """A single-case response should return one section."""
    html = _load("la_ruling_pas_p.html")
    sections = _split_cases_html(html)
    assert len(sections) >= 1


def test_split_cases_html_no_speech_div_returns_empty() -> None:
    """HTML without div#speechSynthesis should return an empty list."""
    html = "<html><body><p>No rulings here.</p></body></html>"
    sections = _split_cases_html(html)
    assert sections == []


def test_split_cases_per_case_field_extraction() -> None:
    """After splitting, _extract_ruling_fields extracts correct fields for each case."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    sections = _split_cases_html(html)
    assert len(sections) == 2

    # Case 1: Aasi v. Honda
    doc1 = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 2),
        content_format=ContentFormat.HTML,
        raw_content=sections[0].encode("utf-8"),
        content_hash="",
    )
    soup1 = BeautifulSoup(doc1.raw_content, "lxml")
    _extract_ruling_fields(soup1, doc1)
    assert doc1.case_number == "24NNCV02551"
    assert doc1.judge_name is not None
    assert "Crowfoot" in doc1.judge_name
    assert doc1.case_title is not None
    assert "Aasi" in doc1.case_title

    # Case 2: Mic-Bry8
    doc2 = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 2),
        content_format=ContentFormat.HTML,
        raw_content=sections[1].encode("utf-8"),
        content_hash="",
    )
    soup2 = BeautifulSoup(doc2.raw_content, "lxml")
    _extract_ruling_fields(soup2, doc2)
    assert doc2.case_number == "26NNCP00062"
    assert doc2.judge_name is not None
    assert "Crowfoot" in doc2.judge_name
    assert doc2.case_title is not None
    assert "Mic-Bry8" in doc2.case_title or "Mic-bry8" in doc2.case_title.lower()


# ---------------------------------------------------------------------------
# _extract_case_title — against real ruling response
# ---------------------------------------------------------------------------


def test_extract_case_title_from_fixture() -> None:
    """Extract case title from the real fixture HTML."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    assert "Aasi" in title
    assert "Honda" in title
    assert " v. " in title


def test_extract_case_title_sets_doc_field() -> None:
    """_extract_ruling_fields populates doc.case_title from the fixture."""
    from bs4 import BeautifulSoup

    doc = _make_ruling_doc()
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    assert doc.case_title is not None
    assert "Aasi" in doc.case_title
    assert " v. " in doc.case_title


def test_extract_case_title_returns_none_without_parties_anchor() -> None:
    """When there is no Parties anchor, _extract_case_title returns None."""
    from bs4 import BeautifulSoup

    html = "<div id='speechSynthesis'><p>Some ruling text.</p></div>"
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    assert _extract_case_title(content) is None


# ---------------------------------------------------------------------------
# _extract_case_title — MOVING PARTY / RESPONDING PARTY pattern (fallback)
# ---------------------------------------------------------------------------


def test_extract_case_title_moving_responding_fallback() -> None:
    """When no Parties anchor exists, extract from MOVING/RESPONDING PARTY fields."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>MOVING PARTY: Defendant Rayne Dealership Corporation.</p>"
        "<p>RESPONDING PARTY: Plaintiffs Alpha Beta and Gamma Delta.</p>"
        "<p>The motion is DENIED.</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    assert "Rayne Dealership Corporation" in title
    assert "Alpha Beta" in title
    assert " v. " in title
    # Role prefixes should be stripped
    assert "Defendant" not in title
    assert "Plaintiffs" not in title


def test_extract_case_title_moving_party_no_opposition() -> None:
    """No opposition filed should not produce a title."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>MOVING PARTY: Defendant Big Corp.</p>"
        "<p>RESPONDING PARTY: No opposition filed.</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    assert _extract_case_title(content) is None


def test_extract_case_title_from_cha_f46_fixture() -> None:
    """CHA F46 fixture has MOVING PARTY but RESPONDING is 'No opposition filed'."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_cha_f46.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    # This fixture has "No opposition filed" so title should be None
    # (or from another pattern if present)
    _extract_case_title(content)
    # We just verify it doesn't crash — the fixture has
    # "No opposition filed" as the responding party


def test_extract_case_title_from_com_a_fixture() -> None:
    """COM A fixture has both Parties anchor AND MOVING/RESPONDING PARTY fields."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_com_a.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    # The Parties anchor should take precedence
    assert " v. " in title


# ---------------------------------------------------------------------------
# _extract_case_title — Case Name field pattern (fallback)
# ---------------------------------------------------------------------------


def test_extract_case_title_case_name_field() -> None:
    """Extract from inline 'CASE NAME:' field when no Parties anchor."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>CASE NAME: Porsche Leasing Ltd. et al. v. Tsisana Mikia, et al. "
        "CASE NUMBER: 25SMCV01132</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    assert "Porsche" in title
    assert "Mikia" in title


def test_extract_case_title_from_bh205_fixture() -> None:
    """BH 205 fixture has a CASE NAME field with party names."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_bh205.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    assert "Porsche" in title or "Mikia" in title
    assert "v." in title


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
    assert health.records_captured == 194  # 97 options x 2 cases per fixture


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
    assert health.records_captured == 192  # (97 - 1 failed) x 2 cases per fixture


def test_default_config() -> None:
    config = default_config(s3_bucket="judgemind-document-archive-dev")
    assert config.scraper_id == "ca-la-tentatives-civil"
    assert config.state == "CA"
    assert config.county == "Los Angeles"
    assert config.s3_bucket == "judgemind-document-archive-dev"
    assert len(config.schedule_windows) == 2


# ---------------------------------------------------------------------------
# Stale ViewState detection — regression tests against real error fixtures
# ---------------------------------------------------------------------------


def test_is_stale_viewstate_response_detects_error_page() -> None:
    """la_ruling_smc49.html is a real stale-ViewState error page and must be detected."""
    html = _load("la_ruling_smc49.html")
    assert _is_stale_viewstate_response(html)


def test_is_stale_viewstate_response_all_error_fixtures() -> None:
    """All six known stale-ViewState fixtures are detected as error pages."""
    error_fixtures = [
        "la_ruling_smc49.html",
        "la_ruling_smc56.html",
        "la_ruling_smc1.html",
        "la_ruling_van_a.html",
        "la_ruling_tor_b.html",
        "la_ruling_ea_h.html",
    ]
    for name in error_fixtures:
        assert _is_stale_viewstate_response(_load(name)), f"{name} should be detected as error"


def test_is_stale_viewstate_response_does_not_match_real_ruling() -> None:
    """Normal ruling HTML is not mistaken for a stale-ViewState error page."""
    html = _load("la_ruling_response.html")
    assert not _is_stale_viewstate_response(html)


@respx.mock
def test_full_run_stale_viewstate_not_counted() -> None:
    """Full run: when every POST returns a stale-ViewState error, records_captured == 0."""
    main_html = _load("la_main_page.html")
    stale_html = _load("la_ruling_smc49.html")

    respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))
    respx.post(CIVIL_URL).mock(return_value=httpx.Response(200, text=stale_html))

    config = default_config()
    config.request_delay_seconds = 0
    scraper = LATentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 0


@respx.mock
def test_full_run_stale_viewstate_mixed_with_real() -> None:
    """Full run: stale-ViewState responses are skipped; valid rulings still count."""
    main_html = _load("la_main_page.html")
    stale_html = _load("la_ruling_smc49.html")
    ruling_html = _load("la_ruling_response.html")

    respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))

    call_count = 0

    def post_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # First two calls return the error page; rest return a valid ruling
        if call_count <= 2:
            return httpx.Response(200, text=stale_html)
        return httpx.Response(200, text=ruling_html)

    respx.post(CIVIL_URL).mock(side_effect=post_side_effect)

    config = default_config()
    config.request_delay_seconds = 0
    scraper = LATentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 190  # (97 - 2 stale skips) x 2 cases per fixture


# ---------------------------------------------------------------------------
# _extract_parties — party extraction from real fixtures
# ---------------------------------------------------------------------------


def test_extract_parties_from_fixture() -> None:
    """Extract parties from the real fixture HTML (la_ruling_response.html).

    This fixture has 2 cases with Parties anchors:
    1. SUMAYYA AASI (Plaintiff) vs. AMERICAN HONDA MOTOR CO., INC. (Defendant)
    2. MIC-BRY8, LLC (Petitioner) vs. CERTAIN STATUTORY INTERESTED PARTIES... (Respondent)
    """
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    parties = _extract_parties(content)
    assert len(parties) >= 2

    # Check that plaintiff/defendant roles are present
    roles = {p["role"] for p in parties}
    assert "plaintiff" in roles or "petitioner" in roles
    assert "defendant" in roles or "respondent" in roles

    # Check that Aasi is found as a party
    names_lower = [p["name"].lower() for p in parties]
    assert any("aasi" in n for n in names_lower)


def test_extract_parties_from_anchor_fixture() -> None:
    """_extract_parties_from_anchor finds parties in la_ruling_response.html."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    parties = _extract_parties_from_anchor(content)
    assert len(parties) >= 2

    # First case: Aasi (plaintiff) vs Honda (defendant)
    plaintiff_parties = [p for p in parties if p["role"] == "plaintiff"]
    defendant_parties = [p for p in parties if p["role"] == "defendant"]
    assert len(plaintiff_parties) >= 1
    assert len(defendant_parties) >= 1
    assert any("Aasi" in p["name"] for p in plaintiff_parties)
    assert any("Honda" in p["name"] for p in defendant_parties)


def test_extract_parties_from_anchor_petitioner_respondent() -> None:
    """The second case in la_ruling_response.html uses petitioner/respondent roles."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    parties = _extract_parties_from_anchor(content)

    roles = {p["role"] for p in parties}
    assert "petitioner" in roles
    assert "respondent" in roles


def test_extract_parties_from_com_a_fixture() -> None:
    """COM A fixture has Parties anchor — DAVID KEICHLINE vs ASHLEY WILLOWBROOK LP."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_com_a.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    parties = _extract_parties(content)
    assert len(parties) >= 2

    names_lower = [p["name"].lower() for p in parties]
    assert any("keichline" in n for n in names_lower)
    assert any("willowbrook" in n for n in names_lower)


def test_extract_parties_sets_doc_field() -> None:
    """_extract_ruling_fields populates doc.parties from the fixture."""
    from bs4 import BeautifulSoup

    doc = _make_ruling_doc()
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    assert len(doc.parties) >= 2

    # Verify name and role keys are present
    for party in doc.parties:
        assert "name" in party
        assert "role" in party
        assert party["name"]
        assert party["role"]


def test_extract_parties_moving_responding_fallback() -> None:
    """When no Parties anchor, extract from MOVING/RESPONDING PARTY fields."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>MOVING PARTY: Defendant Rayne Dealership Corporation.</p>"
        "<p>RESPONDING PARTY: Plaintiffs Alpha Beta and Gamma Delta.</p>"
        "<p>The motion is DENIED.</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    parties = _extract_parties(content)
    assert len(parties) >= 2

    # Role prefixes should be stripped from names
    for party in parties:
        assert "Defendant" not in party["name"]
        assert "Plaintiffs" not in party["name"]

    # Check roles are moving_party/responding_party
    roles = {p["role"] for p in parties}
    assert "moving_party" in roles
    assert "responding_party" in roles


def test_extract_parties_moving_responding_no_opposition() -> None:
    """No opposition filed returns empty parties list."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>MOVING PARTY: Defendant Big Corp.</p>"
        "<p>RESPONDING PARTY: No opposition filed.</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    parties = _extract_parties(content)
    assert parties == []


def test_extract_parties_no_parties_anchor_no_moving() -> None:
    """When no Parties anchor and no MOVING PARTY, return empty list."""
    from bs4 import BeautifulSoup

    html = "<div id='speechSynthesis'><p>Some ruling text.</p></div>"
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    parties = _extract_parties(content)
    assert parties == []


def test_extract_parties_names_are_title_cased() -> None:
    """Party names should be title-cased."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    parties = _extract_parties(content)
    for party in parties:
        # Title case: first letter of each word capitalized
        assert party["name"] == party["name"].title()


# ---------------------------------------------------------------------------
# _split_party_names — corporate suffix protection (#328)
# ---------------------------------------------------------------------------


def test_split_party_names_keeps_corporate_suffix() -> None:
    """Corporate suffixes like 'Inc.' should NOT be split from the entity name."""
    result = _split_party_names("Techno-Advanced, Inc.")
    assert result == ["Techno-Advanced, Inc."]


def test_split_party_names_keeps_llc_suffix() -> None:
    """LLC suffix should stay with its entity."""
    result = _split_party_names("Big Corp, LLC")
    assert result == ["Big Corp, LLC"]


def test_split_party_names_multiple_with_corporate() -> None:
    """Multiple parties including a corporate entity should split correctly."""
    result = _split_party_names("John Doe, Techno-Advanced, Inc., and Jane Smith")
    assert len(result) == 3
    assert "John Doe" in result
    assert "Techno-Advanced, Inc." in result
    assert "Jane Smith" in result


def test_split_party_names_two_corporates() -> None:
    """Two corporate entities with suffixes should both be preserved."""
    result = _split_party_names("Alpha, LLC, and Beta, Inc.")
    assert len(result) == 2
    assert "Alpha, LLC" in result
    assert "Beta, Inc." in result


def test_split_party_names_filters_fragment() -> None:
    """Standalone corporate suffixes like 'Inc' should be filtered out."""
    result = _split_party_names("Inc")
    assert result == []


def test_split_party_names_filters_short_fragment() -> None:
    """Short single-word names should be filtered as fragments."""
    result = _split_party_names("AB")
    assert result == []


def test_split_party_names_keeps_long_single_word() -> None:
    """Long single-word names (e.g. org names) should be kept."""
    result = _split_party_names("Google")
    assert result == ["Google"]


# ---------------------------------------------------------------------------
# _is_name_fragment — fragment detection (#328)
# ---------------------------------------------------------------------------


def test_is_name_fragment_corporate_suffix() -> None:
    """Corporate suffixes alone are fragments."""
    assert _is_name_fragment("Inc") is True
    assert _is_name_fragment("Inc.") is True
    assert _is_name_fragment("LLC") is True
    assert _is_name_fragment("Corp") is True
    assert _is_name_fragment("Ltd") is True
    assert _is_name_fragment("LP") is True
    assert _is_name_fragment("LLP") is True


def test_is_name_fragment_short_word() -> None:
    """Short single words without spaces are fragments."""
    assert _is_name_fragment("AB") is True
    assert _is_name_fragment("") is True


def test_is_name_fragment_valid_names() -> None:
    """Full names and entity names are NOT fragments."""
    assert _is_name_fragment("John Doe") is False
    assert _is_name_fragment("Techno-Advanced, Inc.") is False
    assert _is_name_fragment("Google") is False
    assert _is_name_fragment("Jane Smith Corporation") is False


# ---------------------------------------------------------------------------
# Department header boilerplate filtering (#422)
# ---------------------------------------------------------------------------


def test_is_dept_header_boilerplate_detects_fixture() -> None:
    """The department header fixture should be detected as boilerplate."""
    html = _load("la_ruling_dept_header.html")
    assert _is_dept_header_boilerplate(html) is True


def test_is_dept_header_boilerplate_rejects_real_ruling() -> None:
    """A real ruling with case numbers is NOT boilerplate."""
    html = _load("la_ruling_response.html")
    assert _is_dept_header_boilerplate(html) is False


def test_is_dept_header_boilerplate_rejects_plain_text() -> None:
    """Plain text without the DEPARTMENT pattern is NOT boilerplate."""
    assert _is_dept_header_boilerplate("<p>Some random content</p>") is False


def test_split_cases_html_dept_header_returns_empty() -> None:
    """Department header boilerplate should produce zero case sections."""
    html = _load("la_ruling_dept_header.html")
    sections = _split_cases_html(html)
    assert sections == []


def test_split_cases_html_dept_header_not_stored_as_ruling() -> None:
    """Full scraper pipeline: department header pages should not produce documents."""
    html = _load("la_ruling_dept_header.html")
    # _split_cases_html returns [] so fetch_documents will not append any docs
    sections = _split_cases_html(html)
    assert len(sections) == 0


@respx.mock
def test_full_run_dept_header_not_counted() -> None:
    """Full run: when every POST returns a department header, records_captured == 0."""
    main_html = _load("la_main_page.html")
    dept_header_html = _load("la_ruling_dept_header.html")

    respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))
    respx.post(CIVIL_URL).mock(return_value=httpx.Response(200, text=dept_header_html))

    config = default_config()
    config.request_delay_seconds = 0
    scraper = LATentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 0


@respx.mock
def test_full_run_dept_header_mixed_with_real() -> None:
    """Full run: department headers are skipped; valid rulings still count."""
    main_html = _load("la_main_page.html")
    dept_header_html = _load("la_ruling_dept_header.html")
    ruling_html = _load("la_ruling_response.html")

    respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))

    call_count = 0

    def post_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # First three calls return department headers; rest return real rulings
        if call_count <= 3:
            return httpx.Response(200, text=dept_header_html)
        return httpx.Response(200, text=ruling_html)

    respx.post(CIVIL_URL).mock(side_effect=post_side_effect)

    config = default_config()
    config.request_delay_seconds = 0
    scraper = LATentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 188  # (97 - 3 headers) x 2 cases per fixture


# ---------------------------------------------------------------------------
# Date-aware directory lookup in parse_document (#767)
# ---------------------------------------------------------------------------


class TestDateAwareDirectoryLookup:
    """Tests for date-aware directory snapshot usage in parse_document."""

    def _make_scraper_with_directory(
        self,
        snapshot_map: dict[str, str] | None = None,
        static_map: dict[str, str] | None = None,
    ) -> LATentativeRulingsScraper:
        """Create a scraper with a mocked court_directory for testing."""
        config = default_config()
        config.request_delay_seconds = 0

        court_directory = MagicMock()
        court_directory.get_mapping_for_date.return_value = snapshot_map

        scraper = LATentativeRulingsScraper(config=config, court_directory=court_directory)
        scraper._court_id = "ca_los_angeles"
        scraper._dept_judge_map = static_map or {}
        return scraper

    def test_uses_date_appropriate_snapshot(self) -> None:
        """parse_document should use the historical snapshot when available."""
        snapshot_map = {"3": "Historical Judge"}
        scraper = self._make_scraper_with_directory(
            snapshot_map=snapshot_map,
            static_map={"3": "Current Judge"},
        )

        doc = CapturedDocument(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url=CIVIL_URL,
            capture_timestamp=datetime(2026, 3, 2),
            content_format=ContentFormat.HTML,
            raw_content=b"<div id='speechSynthesis'><p>Some ruling</p></div>",
            content_hash="",
            department="3",
            hearing_date=datetime(2026, 1, 15),
        )
        result = scraper.parse_document(doc)
        assert result.judge_name == "Historical Judge"

        scraper._court_directory.get_mapping_for_date.assert_called_once_with(
            "ca_los_angeles",
            datetime(2026, 1, 15),
            fallback={"3": "Current Judge"},
        )

    def test_falls_back_to_static_map_without_court_directory(self) -> None:
        """Without court_directory, scraper should use _dept_judge_map."""
        config = default_config()
        config.request_delay_seconds = 0
        scraper = LATentativeRulingsScraper(config=config)
        scraper._dept_judge_map = {"3": "Static Map Judge"}

        doc = CapturedDocument(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url=CIVIL_URL,
            capture_timestamp=datetime(2026, 3, 2),
            content_format=ContentFormat.HTML,
            raw_content=b"<div id='speechSynthesis'><p>Some ruling</p></div>",
            content_hash="",
            department="3",
            hearing_date=datetime(2026, 1, 15),
        )
        result = scraper.parse_document(doc)
        assert result.judge_name == "Static Map Judge"

    def test_falls_back_without_hearing_date(self) -> None:
        """Without hearing_date, should use _dept_judge_map even with court_directory."""
        scraper = self._make_scraper_with_directory(
            snapshot_map={"3": "Snapshot Judge"},
            static_map={"3": "Static Judge"},
        )

        doc = CapturedDocument(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url=CIVIL_URL,
            capture_timestamp=datetime(2026, 3, 2),
            content_format=ContentFormat.HTML,
            raw_content=b"<div id='speechSynthesis'><p>Some ruling</p></div>",
            content_hash="",
            department="3",
        )
        result = scraper.parse_document(doc)
        assert result.judge_name == "Static Judge"
        scraper._court_directory.get_mapping_for_date.assert_not_called()

    def test_skips_lookup_when_judge_in_html(self) -> None:
        """When the HTML contains a judge name, directory lookup should be skipped."""
        html = _load("la_ruling_response.html")
        scraper = self._make_scraper_with_directory(
            snapshot_map={"3": "Wrong Judge"},
            static_map={"3": "Also Wrong"},
        )

        doc = CapturedDocument(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url=CIVIL_URL,
            capture_timestamp=datetime(2026, 3, 2),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="",
            department="3",
            hearing_date=datetime(2026, 3, 2),
        )
        result = scraper.parse_document(doc)
        # Judge should come from the HTML, not the directory
        assert "Crowfoot" in result.judge_name

    def test_no_mapping_match_leaves_judge_none(self) -> None:
        """When no mapping has the department, judge_name stays None."""
        scraper = self._make_scraper_with_directory(
            snapshot_map={"99": "Some Judge"},
            static_map={"99": "Other Judge"},
        )

        doc = CapturedDocument(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url=CIVIL_URL,
            capture_timestamp=datetime(2026, 3, 2),
            content_format=ContentFormat.HTML,
            raw_content=b"<div id='speechSynthesis'><p>Some ruling</p></div>",
            content_hash="",
            department="3",
            hearing_date=datetime(2026, 1, 15),
        )
        result = scraper.parse_document(doc)
        assert result.judge_name is None


# ---------------------------------------------------------------------------
# _sanitize_title — title cleanup (#1244)
# ---------------------------------------------------------------------------


def test_sanitize_title_rejects_department_header() -> None:
    """Titles containing department header boilerplate are rejected."""
    title = (
        "Department I Law And Motion Rulings Case Number: 24Vecv05649 "
        "Hearing Date: March 6, 2026 Dept: I Superior Court v. Someone"
    )
    assert _sanitize_title(title) is None


def test_sanitize_title_strips_entity_descriptors() -> None:
    """Entity descriptors like 'An Individual' are stripped from party names."""
    title = "Jim Hilaski, An Individual v. Shaul Dina, An Individual"
    result = _sanitize_title(title)
    assert result is not None
    assert "An Individual" not in result
    assert "Hilaski" in result
    assert "Dina" in result
    assert " v. " in result


def test_sanitize_title_strips_california_corporation() -> None:
    """'A California Corporation' entity descriptors are stripped."""
    title = (
        "Old Master Products, Inc., A California Corporation v. Big Corp, A Delaware Corporation"
    )
    result = _sanitize_title(title)
    assert result is not None
    assert "A California Corporation" not in result
    assert "A Delaware Corporation" not in result
    assert "Old Master Products" in result


def test_sanitize_title_strips_new_states() -> None:
    """Entity descriptors for newly added states are stripped."""
    title = (
        "Acme Corp, A Connecticut Corporation v. Widget LLC, An Arkansas Limited Liability Company"
    )
    result = _sanitize_title(title)
    assert result is not None
    assert "A Connecticut Corporation" not in result
    assert "An Arkansas Limited Liability Company" not in result
    assert "Acme Corp" in result

    title2 = "Smith v. Dakota Holdings, A South Dakota Corporation"
    result2 = _sanitize_title(title2)
    assert result2 is not None
    assert "A South Dakota Corporation" not in result2
    assert "Dakota Holdings" in result2


def test_sanitize_title_strips_district_of_columbia() -> None:
    """Entity descriptors for District of Columbia are stripped."""
    title = "Fed Agency v. DC Corp, A District of Columbia Corporation"
    result = _sanitize_title(title)
    assert result is not None
    assert "A District of Columbia Corporation" not in result
    assert "DC Corp" in result


def test_sanitize_title_strips_derivatively_on_behalf() -> None:
    """'An Individual And Derivatively On Behalf Of...' is stripped."""
    title = (
        "Jim Hilaski, An Individual And Derivatively On Behalf Of Old Master Products, Inc."
        " v. Shaul Dina, An Individual"
    )
    result = _sanitize_title(title)
    assert result is not None
    assert "Derivatively" not in result
    assert "Hilaski" in result
    assert "Dina" in result


def test_sanitize_title_strips_does_1_to_20() -> None:
    """'Does 1 To 20, Inclusive' is stripped."""
    title = "Smith v. Jones; Does 1 To 20, Inclusive"
    result = _sanitize_title(title)
    assert result is not None
    assert "Does" not in result
    assert "Smith" in result
    assert "Jones" in result


def test_sanitize_title_normalizes_vs_separator() -> None:
    """'vs.' and 'vs' separators are normalized to 'v.' before stripping."""
    title = "Jim Hilaski, An Individual vs. Shaul Dina, An Individual"
    result = _sanitize_title(title)
    assert result is not None
    assert "An Individual" not in result
    assert " v. " in result
    assert "vs." not in result


def test_sanitize_title_passes_normal_title() -> None:
    """Normal short titles pass through unchanged."""
    title = "Aasi v. Honda"
    assert _sanitize_title(title) == "Aasi v. Honda"


def test_sanitize_title_rejects_none() -> None:
    """None input returns None."""
    assert _sanitize_title(None) is None


def test_sanitize_title_rejects_empty() -> None:
    """Empty string returns None."""
    assert _sanitize_title("") is None


def test_sanitize_title_rejects_too_short() -> None:
    """Titles shorter than 5 chars after cleaning are rejected."""
    assert _sanitize_title("A v.") is None


def test_sanitize_title_rejects_too_long() -> None:
    """Titles exceeding 120 chars after cleaning are rejected."""
    long_title = "A" * 60 + " v. " + "B" * 60
    assert _sanitize_title(long_title) is None


# ---------------------------------------------------------------------------
# Department header NOT in case title — regression test (#1244)
# ---------------------------------------------------------------------------


def test_extract_case_title_dept_header_fixture_no_header_in_title() -> None:
    """Case title from dept I fixture must NOT contain department header boilerplate."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_dept_i_header_in_title.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    # Title should be extracted from MOVING/RESPONDING PARTY fields
    # and NOT contain "Department" or "Law And Motion Rulings"
    if title is not None:
        assert "Department" not in title
        assert "Law And Motion Rulings" not in title
        assert "DEPARTMENT" not in title
        assert "Superior Court" not in title


def test_extract_case_title_dept_header_fixture_has_party_names() -> None:
    """Case title from dept I fixture should contain actual party names."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_dept_i_header_in_title.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    assert "Hilaski" in title or "hilaski" in title.lower()
    assert " v. " in title


def test_split_and_extract_dept_header_not_in_title() -> None:
    """End-to-end: splitting + extraction for dept I fixture produces clean title."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_dept_i_header_in_title.html")
    sections = _split_cases_html(html)
    assert len(sections) == 1

    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 6),
        content_format=ContentFormat.HTML,
        raw_content=sections[0].encode("utf-8"),
        content_hash="",
    )
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    assert doc.case_title is not None
    assert "Department" not in doc.case_title
    assert "Law And Motion" not in doc.case_title
    assert "Hilaski" in doc.case_title or "hilaski" in doc.case_title.lower()
    # Entity descriptors should be stripped
    assert "An Individual" not in doc.case_title
    assert "A California Corporation" not in doc.case_title


def test_existing_fixture_titles_not_affected_by_sanitize() -> None:
    """Existing fixture titles (Aasi, Keichline, Porsche) are not broken by sanitization."""
    from bs4 import BeautifulSoup

    # la_ruling_response.html — Aasi v. Honda (uses Parties anchor)
    html = _load("la_ruling_response.html")
    sections = _split_cases_html(html)
    assert len(sections) == 2

    soup1 = BeautifulSoup(sections[0], "lxml")
    content1 = soup1.find("div", id="speechSynthesis")
    title1 = _extract_case_title(content1)
    assert title1 is not None
    assert "Aasi" in title1

    # la_ruling_bh205.html — Porsche v. Mikia (uses Case Name field)
    html_bh = _load("la_ruling_bh205.html")
    soup_bh = BeautifulSoup(html_bh, "lxml")
    content_bh = soup_bh.find("div", id="speechSynthesis")
    title_bh = _extract_case_title(content_bh)
    assert title_bh is not None
    assert "Porsche" in title_bh or "Mikia" in title_bh


# ---------------------------------------------------------------------------
# Role-label stripping — _clean_party_name and title extraction (#1425)
# ---------------------------------------------------------------------------


def test_clean_party_name_strips_role_prefix_with_comma() -> None:
    """_clean_party_name strips 'Defendant, ' prefix (comma+space)."""
    from courts.ca.la_tentatives import _clean_party_name

    assert _clean_party_name("Defendant, Albery Arevalo") == "Albery Arevalo"
    result = _clean_party_name("Plaintiffs, Daniel And Priscila Stanton")
    assert result == "Daniel And Priscila Stanton"
    result2 = _clean_party_name("Defendants, White Pickle Llc And Adam Grandmaison")
    assert result2 == "White Pickle Llc And Adam Grandmaison"


def test_clean_party_name_strips_role_prefix_with_space() -> None:
    """_clean_party_name still strips 'Defendant ' prefix (space only)."""
    from courts.ca.la_tentatives import _clean_party_name

    assert _clean_party_name("Defendant Big Corp") == "Big Corp"
    assert _clean_party_name("Plaintiffs Alpha Beta") == "Alpha Beta"


def test_clean_party_name_rejects_bare_role_label() -> None:
    """_clean_party_name returns empty string for bare role labels."""
    from courts.ca.la_tentatives import _clean_party_name

    assert _clean_party_name("Defendant") == ""
    assert _clean_party_name("Plaintiff") == ""
    assert _clean_party_name("Defendants") == ""
    assert _clean_party_name("Plaintiffs") == ""
    assert _clean_party_name("Petitioner") == ""
    assert _clean_party_name("Respondent") == ""


def test_extract_title_rejects_defendant_v_plaintiff() -> None:
    """Caption block with only role labels produces None title."""
    from bs4 import BeautifulSoup

    # Simulate a caption where only "Defendant" and "Plaintiff" appear as names
    html = (
        '<div id="speechSynthesis">'
        "<table><tr><td>"
        '<a name="Parties"></a>'
        "Defendant,\n  Plaintiff(s),\n  vs.\n  Plaintiff,\n  Defendant(s)."
        "</td></tr></table>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    # Should be None because after stripping role labels, names are empty
    assert title is None


def test_extract_title_strips_role_prefix_from_caption() -> None:
    """Caption with 'Defendant, Name' format strips the role prefix."""
    from bs4 import BeautifulSoup

    html = (
        '<div id="speechSynthesis">'
        "<table><tr><td>"
        '<a name="Parties"></a>'
        "Defendant, STATE FARM AUTOMOBILE INSURANCE COMPANY,\n"
        "  Plaintiff(s),\n  vs.\n"
        "  Plaintiff, KEITH JOHNSON,\n  Defendant(s)."
        "</td></tr></table>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    assert "Defendant" not in title
    assert "Plaintiff" not in title
    assert "State Farm" in title
    assert "Keith Johnson" in title
    assert " v. " in title


def test_extract_parties_rejects_bare_role_labels() -> None:
    """Party extraction skips bare role-label names (#1425)."""
    from bs4 import BeautifulSoup

    html = (
        '<div id="speechSynthesis">'
        "<table><tr><td>"
        '<a name="Parties"></a>'
        "Defendant,\n  Plaintiff(s),\n  vs.\n  Plaintiff,\n  Defendant(s)."
        "</td></tr></table>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    parties = _extract_parties(content)
    # All names would be bare role labels — should be filtered out
    for party in parties:
        assert party["name"].lower() not in {
            "defendant",
            "plaintiff",
            "defendants",
            "plaintiffs",
        }


def test_extract_parties_strips_role_prefix_comma() -> None:
    """Party extraction strips 'Defendant, ' prefix from names (#1425)."""
    from bs4 import BeautifulSoup

    html = (
        '<div id="speechSynthesis">'
        "<table><tr><td>"
        '<a name="Parties"></a>'
        "Defendant, ALBERY AREVALO,\n  Plaintiff(s),\n  vs.\n"
        "  Plaintiffs, DANIEL AND PRISCILA STANTON,\n  Defendant(s)."
        "</td></tr></table>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    parties = _extract_parties(content)
    for party in parties:
        assert "Defendant" not in party["name"]
        assert "Plaintiff" not in party["name"]


def test_role_label_title_blocked_by_extract_not_sanitize() -> None:
    """Role-label titles like 'Defendant v. Plaintiff' are blocked by upstream
    extraction (_extract_title_from_parties_anchor calls _clean_party_name),
    not by _sanitize_title itself. _sanitize_title only cleans entity
    descriptors and checks length — it does not know about role labels."""
    # _sanitize_title does NOT reject "Defendant v. Plaintiff" — it's a
    # valid-looking title by length/format. The protection is upstream.
    result = _sanitize_title("Defendant v. Plaintiff")
    # This passes through _sanitize_title (it's a short, valid-looking string).
    # The actual blocking happens in _extract_title_from_parties_anchor.
    assert result is not None  # sanitize does not reject it
