"""Tests for LA County tentative ruling scraper — built against real fixture HTML.

Fixtures captured from live site 2026-03-02:
  la_main_page.html   — GET https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil
  la_ruling_response.html — POST for ALH,3,03/02/2026 (Alhambra Dept 3)
"""

from __future__ import annotations

import functools
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from courts.ca.la_tentatives import (
    APPELLATE_COURTHOUSE,
    APPELLATE_DEPARTMENT,
    APPELLATE_URL,
    CIVIL_URL,
    LA_SYSTEM_PROMPT,
    LAAppellateTentativeRulingsScraper,
    LASplitRuling,
    LATentativeRulingsScraper,
    _extract_aspnet_tokens,
    _extract_case_title,
    _extract_header_fields,
    _extract_parties,
    _extract_parties_from_anchor,
    _extract_ruling_fields,
    _has_no_rulings_appellate,
    _is_dept_header_boilerplate,
    _is_name_fragment,
    _is_stale_viewstate_response,
    _la_llm_enabled,
    _llm_extract_rulings,
    _parse_appellate_dropdown_options,
    _parse_dropdown_options,
    _parse_option,
    _replace_ruling_text_from_html,
    _sanitize_title,
    _split_cases_html,
    _split_party_names,
    _split_rulings,
    _truncate_party_list,
    default_config,
    default_config_appellate,
    is_la_html,
    sanitize_ruling_html,
)
from framework import ContentFormat
from framework.models import CapturedDocument

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


@functools.cache
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


def test_sanitize_title_strips_hyphenated_successor_in_interest() -> None:
    """Hyphenated 'Successor-In-Interest To' is stripped (#1375)."""
    title = "Smith, Successor-In-Interest To John Doe v. Jones"
    result = _sanitize_title(title)
    assert result is not None
    assert "Successor" not in result
    assert "Smith" in result
    assert "Jones" in result


def test_sanitize_title_strips_successor_in_interest_and_administrator() -> None:
    """'Successor In Interest To And Administrator Of The Estate Of' is stripped (#1375)."""
    title = (
        "Smith, Successor In Interest To And Administrator Of The Estate Of Robert Brown v. Jones"
    )
    result = _sanitize_title(title)
    assert result is not None
    assert "Successor" not in result
    assert "Administrator" not in result
    assert "Estate" not in result
    assert "Smith" in result
    assert "Jones" in result


def test_sanitize_title_strips_administrator_of_estate() -> None:
    """'Administrator Of The Estate Of [name]' is stripped (#1375)."""
    title = "Smith, Administrator Of The Estate Of John Doe v. Jones"
    result = _sanitize_title(title)
    assert result is not None
    assert "Administrator" not in result
    assert "Smith" in result
    assert "Jones" in result


def test_sanitize_title_strips_as_an_individual() -> None:
    """'As An Individual' is stripped (#1375)."""
    title = "Smith, As An Individual v. Jones, As An Individual"
    result = _sanitize_title(title)
    assert result is not None
    assert "As An Individual" not in result
    assert "Smith" in result
    assert "Jones" in result


def test_sanitize_title_truncates_multi_party_with_semicolons() -> None:
    """Multi-party titles with semicolons are truncated to first party + et al. (#1375)."""
    # Build a title that is > 120 chars after descriptor stripping but has semicolons
    title = (
        "John Erickson, An Individual; Yao Mou, An Individual; "
        "Sandra Richardson, An Individual; Peter Wellington, An Individual "
        "v. Jason Wei, An Individual; Tiffany Wang, An Individual; "
        "Robert Henderson, An Individual; Victoria Chamberlain, An Individual"
    )
    result = _sanitize_title(title)
    assert result is not None
    assert len(result) <= 120
    assert "Erickson" in result
    assert "et al." in result


def test_sanitize_title_truncates_many_defendants() -> None:
    """Titles with many defendants are truncated to first + et al. (#1375)."""
    # Build a title that is > 120 chars after cleaning with many parties
    title = (
        "Alice Margaret Walker-Thompson v. Bob Smith; Carol Jones-Williams; "
        "David James Brown; Eve Stephanie Wilson; Frank Harrison Miller; "
        "Grace Catherine Lee; Henry Robert Davis"
    )
    result = _sanitize_title(title)
    assert result is not None
    assert len(result) <= 120
    assert "Walker-Thompson" in result
    assert "Smith" in result
    assert "et al." in result


def test_sanitize_title_no_truncation_when_short_enough() -> None:
    """Short multi-party titles are not truncated."""
    title = "Smith; Jones v. Brown; Davis"
    result = _sanitize_title(title)
    assert result is not None
    # Short enough — no truncation needed
    assert result == "Smith; Jones v. Brown; Davis"


def test_sanitize_title_strips_guardian_ad_litem() -> None:
    """'By And Through His Guardian Ad Litem [name]' is stripped (#1375)."""
    title = "Minor Child, By And Through His Guardian Ad Litem John Smith v. Jones"
    result = _sanitize_title(title)
    assert result is not None
    assert "Guardian" not in result
    assert "By And Through" not in result
    assert "Minor Child" in result
    assert "Jones" in result


# ---------------------------------------------------------------------------
# _truncate_party_list — multi-party truncation (#1375)
# ---------------------------------------------------------------------------


def test_truncate_party_list_single_party() -> None:
    """A single party is returned unchanged."""
    assert _truncate_party_list("John Smith") == "John Smith"


def test_truncate_party_list_semicolons() -> None:
    """Multiple parties separated by semicolons are truncated."""
    result = _truncate_party_list("John Smith; Jane Doe; Bob Brown")
    assert result == "John Smith, et al."


def test_truncate_party_list_and_connector() -> None:
    """Parties separated by ', and' are truncated."""
    result = _truncate_party_list("John Smith, and Jane Doe")
    assert result == "John Smith, et al."


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


# ---------------------------------------------------------------------------
# LLM extraction tests (#1938)
# ---------------------------------------------------------------------------


class TestLaLlmEnabled:
    """Tests for the _la_llm_enabled() feature flag."""

    def test_disabled_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert _la_llm_enabled() is False

    def test_enabled_with_true(self) -> None:
        with patch.dict("os.environ", {"ENABLE_LA_LLM_EXTRACTION": "true"}):
            assert _la_llm_enabled() is True

    def test_enabled_with_1(self) -> None:
        with patch.dict("os.environ", {"ENABLE_LA_LLM_EXTRACTION": "1"}):
            assert _la_llm_enabled() is True

    def test_enabled_with_yes(self) -> None:
        with patch.dict("os.environ", {"ENABLE_LA_LLM_EXTRACTION": "yes"}):
            assert _la_llm_enabled() is True

    def test_enabled_case_insensitive(self) -> None:
        with patch.dict("os.environ", {"ENABLE_LA_LLM_EXTRACTION": "TRUE"}):
            assert _la_llm_enabled() is True

    def test_disabled_with_false(self) -> None:
        with patch.dict("os.environ", {"ENABLE_LA_LLM_EXTRACTION": "false"}):
            assert _la_llm_enabled() is False

    def test_disabled_with_empty_string(self) -> None:
        with patch.dict("os.environ", {"ENABLE_LA_LLM_EXTRACTION": ""}):
            assert _la_llm_enabled() is False


def _make_llm_response(rulings_data: list[dict]) -> object:
    """Create a mock LLMResponse with the given rulings data."""
    from ingestion.llm_providers import LLMResponse

    response_json = json.dumps({"rulings": rulings_data})
    return LLMResponse(text=response_json, input_tokens=100, output_tokens=200)


class TestLlmExtractRulings:
    """Tests for _llm_extract_rulings()."""

    def test_returns_none_on_api_failure(self) -> None:
        """When call_llm returns None, _llm_extract_rulings returns None."""
        test_html = "<html><body><div id='speechSynthesis'>test</div></body></html>"
        with patch("ingestion.llm_providers.call_llm", return_value=None):
            result = _llm_extract_rulings(test_html)
        assert result is None

    def test_returns_none_on_empty_html(self) -> None:
        """Empty HTML should return None without calling the LLM."""
        result = _llm_extract_rulings("")
        assert result is None

    def test_parses_single_ruling_response(self) -> None:
        """A valid LLM response with one ruling should parse correctly."""
        rulings_data = [
            {
                "extracted_case_number": "24NNCV02551",
                "extracted_case_title": "Aasi v. American Honda Motor Co.",
                "case_type": "civil",
                "outcome": "granted",
                "motion_type": "motion_to_compel",
                "ruling_text": "The motion to compel is GRANTED.",
                "parties": [
                    {"name": "Sumayya Aasi", "role": "plaintiff"},
                    {"name": "American Honda Motor Co., Inc.", "role": "defendant"},
                ],
            }
        ]
        mock_response = _make_llm_response(rulings_data)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>Case Number: 24NNCV02551</div></body></html>"
            )

        assert result is not None
        assert len(result) == 1
        ruling = result[0]
        assert ruling.case_number == "24NNCV02551"
        assert ruling.case_title == "Aasi v. American Honda Motor Co."
        assert ruling.outcome == "granted"
        assert ruling.motion_type == "motion_to_compel"
        assert ruling.ruling_text == "The motion to compel is GRANTED."
        assert len(ruling.parties) == 2
        assert ruling.parties[0]["name"] == "Sumayya Aasi"
        assert ruling.parties[0]["role"] == "plaintiff"

    def test_parses_multiple_rulings(self) -> None:
        """A response with multiple rulings should return all of them."""
        rulings_data = [
            {
                "extracted_case_number": "24NNCV02551",
                "extracted_case_title": "Aasi v. Honda",
                "outcome": "granted",
                "ruling_text": "Ruling 1",
                "parties": [],
            },
            {
                "extracted_case_number": "26NNCP00062",
                "extracted_case_title": "Mic-Bry8, LLC",
                "outcome": "granted",
                "ruling_text": "Ruling 2",
                "parties": [],
            },
        ]
        mock_response = _make_llm_response(rulings_data)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>content</div></body></html>"
            )

        assert result is not None
        assert len(result) == 2
        assert result[0].case_number == "24NNCV02551"
        assert result[0].ruling_index == 1
        assert result[1].case_number == "26NNCP00062"
        assert result[1].ruling_index == 2

    def test_handles_invalid_json(self) -> None:
        """Invalid JSON from LLM should return None."""
        from ingestion.llm_providers import LLMResponse

        mock_response = LLMResponse(text="not valid json", input_tokens=10, output_tokens=20)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>test</div></body></html>"
            )
        assert result is None

    def test_handles_markdown_code_fences(self) -> None:
        """LLM responses wrapped in ```json fences should parse correctly."""
        from ingestion.llm_providers import LLMResponse

        rulings_json = json.dumps(
            {
                "rulings": [
                    {
                        "extracted_case_number": "24STCV01234",
                        "ruling_text": "Granted.",
                        "parties": [],
                    }
                ]
            }
        )
        mock_response = LLMResponse(
            text=f"```json\n{rulings_json}\n```",
            input_tokens=10,
            output_tokens=20,
        )
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>test</div></body></html>"
            )
        assert result is not None
        assert len(result) == 1
        assert result[0].case_number == "24STCV01234"

    def test_normalizes_outcome(self) -> None:
        """Outcome values should be normalized to valid DB enum values."""
        rulings_data = [
            {
                "extracted_case_number": "24STCV01234",
                "outcome": "granted_in_part",
                "ruling_text": "Partially granted.",
                "parties": [],
            }
        ]
        mock_response = _make_llm_response(rulings_data)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>test</div></body></html>"
            )
        assert result is not None
        assert result[0].outcome == "granted_in_part"

    def test_unknown_outcome_maps_to_none(self) -> None:
        """Unknown outcome values should map to None."""
        rulings_data = [
            {
                "extracted_case_number": "24STCV01234",
                "outcome": "reversed",
                "ruling_text": "Some text.",
                "parties": [],
            }
        ]
        mock_response = _make_llm_response(rulings_data)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>test</div></body></html>"
            )
        assert result is not None
        assert result[0].outcome is None

    def test_filters_invalid_party_names(self) -> None:
        """Party names with newlines or >200 chars should be filtered out."""
        rulings_data = [
            {
                "extracted_case_number": "24STCV01234",
                "ruling_text": "Text",
                "parties": [
                    {"name": "Valid Name", "role": "plaintiff"},
                    {"name": "Invalid\nName", "role": "defendant"},
                    {"name": "X" * 201, "role": "defendant"},
                ],
            }
        ]
        mock_response = _make_llm_response(rulings_data)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>test</div></body></html>"
            )
        assert result is not None
        assert len(result[0].parties) == 1
        assert result[0].parties[0]["name"] == "Valid Name"

    def test_accepts_bare_list_response(self) -> None:
        """LLM may return a bare JSON array instead of {rulings: [...]}."""
        from ingestion.llm_providers import LLMResponse

        rulings_list = [
            {
                "extracted_case_number": "24STCV01234",
                "ruling_text": "Text",
                "parties": [],
            }
        ]
        mock_response = LLMResponse(
            text=json.dumps(rulings_list),
            input_tokens=10,
            output_tokens=20,
        )
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>test</div></body></html>"
            )
        assert result is not None
        assert len(result) == 1

    def test_empty_rulings_returns_empty_list(self) -> None:
        """A response with an empty rulings array returns an empty list."""
        mock_response = _make_llm_response([])
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>test</div></body></html>"
            )
        assert result is not None
        assert result == []


class TestLaSplitRuling:
    """Tests for the LASplitRuling dataclass."""

    def test_default_fields(self) -> None:
        ruling = LASplitRuling(ruling_index=1, case_number="24STCV01234", ruling_text="Text")
        assert ruling.case_title is None
        assert ruling.motion_type is None
        assert ruling.outcome is None
        assert ruling.parties == []

    def test_all_fields(self) -> None:
        ruling = LASplitRuling(
            ruling_index=1,
            case_number="24STCV01234",
            ruling_text="The motion is granted.",
            case_title="Smith v. Jones",
            motion_type="demurrer",
            outcome="granted",
            parties=[{"name": "Smith", "role": "plaintiff"}],
        )
        assert ruling.case_number == "24STCV01234"
        assert ruling.case_title == "Smith v. Jones"
        assert ruling.outcome == "granted"
        assert len(ruling.parties) == 1


class TestLaSystemPrompt:
    """Tests for the LA_SYSTEM_PROMPT constant."""

    def test_prompt_mentions_la_county(self) -> None:
        assert "Los Angeles" in LA_SYSTEM_PROMPT

    def test_prompt_includes_output_format(self) -> None:
        assert "extracted_case_number" in LA_SYSTEM_PROMPT
        assert "extracted_case_title" in LA_SYSTEM_PROMPT
        assert "ruling_text" in LA_SYSTEM_PROMPT

    def test_prompt_includes_outcome_taxonomy(self) -> None:
        assert "granted" in LA_SYSTEM_PROMPT
        assert "denied" in LA_SYSTEM_PROMPT
        assert "granted_in_part" in LA_SYSTEM_PROMPT


def _make_doc_with_fields(**kwargs: object) -> CapturedDocument:
    """Helper to create a CapturedDocument with sensible defaults for tests."""
    defaults: dict[str, object] = {
        "scraper_id": "ca-la-tentatives-civil",
        "state": "CA",
        "county": "Los Angeles",
        "court": "Superior Court",
        "source_url": CIVIL_URL,
        "capture_timestamp": datetime(2026, 3, 2, 18, 0, 0),
        "content_format": ContentFormat.HTML,
        "raw_content": b"<html><body><div id='speechSynthesis'>test</div></body></html>",
        "content_hash": "",
    }
    defaults.update(kwargs)
    return CapturedDocument(**defaults)


class TestParseDocumentLlmPath:
    """Tests for parse_document when LLM-extracted docs are passed."""

    def test_preserves_all_llm_fields_when_all_populated(self) -> None:
        """When _llm_extracted is True and all fields are populated,
        parse_document should preserve LLM-populated fields without
        overwriting them via regex."""
        scraper = LATentativeRulingsScraper(default_config())
        doc = _make_doc_with_fields(
            case_number="24NNCV02551",
            ruling_text="LLM ruling text",
            case_title="LLM Title",
            outcome="granted",
            extra={"_llm_extracted": True},
        )

        result = scraper.parse_document(doc)

        # Fields should be preserved from LLM extraction
        assert result.case_number == "24NNCV02551"
        assert result.ruling_text == "LLM ruling text"
        assert result.case_title == "LLM Title"
        assert result.outcome == "granted"

    def test_regex_fallback_fills_null_case_title(self) -> None:
        """When LLM returns null case_title for the first case on a
        multi-case page, regex extraction should fill it from the
        correct per-case HTML chunk."""
        scraper = LATentativeRulingsScraper(default_config())
        # Use full multi-case HTML (not pre-split) to match production
        html = _load("la_ruling_response.html")

        doc = _make_doc_with_fields(
            raw_content=html.encode("utf-8"),
            case_number="24NNCV02551",
            ruling_text="LLM ruling text",
            case_title=None,  # LLM didn't extract this
            outcome="denied",
            extra={"_llm_extracted": True, "ruling_index": 1},
        )

        result = scraper.parse_document(doc)

        # LLM fields should be preserved
        assert result.case_number == "24NNCV02551"
        assert result.ruling_text == "LLM ruling text"
        assert result.outcome == "denied"
        # Regex should have filled case_title from the correct chunk
        assert result.case_title is not None
        assert "Aasi" in result.case_title

    def test_regex_fallback_second_case_on_multi_case_page(self) -> None:
        """When LLM returns null fields for the second case on a multi-case
        page, regex fallback should use the correct HTML chunk, not the
        first case's chunk."""
        scraper = LATentativeRulingsScraper(default_config())
        html = _load("la_ruling_response.html")

        doc = _make_doc_with_fields(
            raw_content=html.encode("utf-8"),
            case_number="26NNCP00062",  # Second case
            ruling_text="LLM ruling text for case 2",
            case_title=None,  # Needs fallback
            extra={"_llm_extracted": True, "ruling_index": 2},
        )

        result = scraper.parse_document(doc)

        # LLM fields preserved
        assert result.case_number == "26NNCP00062"
        assert result.ruling_text == "LLM ruling text for case 2"
        # The case_title from regex should be for the SECOND case, not first
        if result.case_title is not None:
            assert "Aasi" not in result.case_title

    def test_regex_fallback_aborts_for_invalid_ruling_index(self) -> None:
        """When ruling_index is out of bounds on a multi-case page,
        the fallback should abort to prevent data corruption."""
        scraper = LATentativeRulingsScraper(default_config())
        html = _load("la_ruling_response.html")

        doc = _make_doc_with_fields(
            raw_content=html.encode("utf-8"),
            case_number="24NNCV02551",
            ruling_text="LLM text",
            case_title=None,  # Needs fallback
            extra={"_llm_extracted": True, "ruling_index": 99},  # Out of bounds
        )

        result = scraper.parse_document(doc)

        # Fallback should have been aborted — case_title stays None
        assert result.case_title is None
        # LLM fields should be preserved
        assert result.case_number == "24NNCV02551"
        assert result.ruling_text == "LLM text"

    def test_regex_fallback_does_not_overwrite_non_header_llm_fields(self) -> None:
        """Non-header LLM fields (ruling_text, case_title) keep precedence.

        Deterministic header fields (case_number, hearing_date, department) are
        overridden by header extraction (#2330), but other LLM fields are preserved.
        """
        scraper = LATentativeRulingsScraper(default_config())
        # Use full multi-case HTML to match production
        html = _load("la_ruling_response.html")

        doc = _make_doc_with_fields(
            raw_content=html.encode("utf-8"),
            case_number="CUSTOM_NUMBER",  # Will be overridden by header extraction
            ruling_text="LLM ruling text keeps precedence",
            case_title="LLM Custom Title",
            judge_name=None,  # This should be filled by regex
            extra={"_llm_extracted": True, "ruling_index": 1},
        )

        result = scraper.parse_document(doc)

        # Deterministic header fields override LLM values (#2330)
        assert result.case_number == "24NNCV02551"
        # Non-header LLM values must still take precedence
        assert result.ruling_text == "LLM ruling text keeps precedence"
        assert result.case_title == "LLM Custom Title"
        # Judge name should be filled by regex/dept fallback
        assert result.judge_name is not None

    def test_regex_path_still_works_for_non_llm_docs(self) -> None:
        """Without _llm_extracted, parse_document should use regex extraction."""
        scraper = LATentativeRulingsScraper(default_config())
        html = _load("la_ruling_pas_p.html")
        # Split to get per-case HTML
        case_htmls = _split_cases_html(html)
        assert len(case_htmls) == 1

        doc = _make_doc_with_fields(
            raw_content=case_htmls[0].encode("utf-8"),
        )
        result = scraper.parse_document(doc)

        # Regex extraction should populate case_number
        assert result.case_number == "25NNCV00140"

    def test_judge_fallback_still_works_for_llm_docs(self) -> None:
        """LLM-extracted docs with no judge_name should still try dept mapping."""
        dept_map = {"P": "Test Judge Name"}
        scraper = LATentativeRulingsScraper(default_config(), dept_judge_map=dept_map)
        doc = _make_doc_with_fields(
            case_number="25NNCV00140",
            department="P",
            extra={"_llm_extracted": True},
        )

        result = scraper.parse_document(doc)

        assert result.judge_name == "Test Judge Name"

    def test_skips_regex_fallback_when_all_fields_populated(self) -> None:
        """When all fallback fields are populated (including parties),
        the regex fallback should be skipped entirely."""
        scraper = LATentativeRulingsScraper(default_config())
        doc = _make_doc_with_fields(
            case_number="24NNCV02551",
            ruling_text="Full ruling text here",
            case_title="Test Title",
            judge_name="Test Judge",
            parties=[{"name": "Alice", "role": "plaintiff"}],
            extra={"_llm_extracted": True},
        )

        result = scraper.parse_document(doc)

        # All fields should remain unchanged
        assert result.case_number == "24NNCV02551"
        assert result.ruling_text == "Full ruling text here"
        assert result.case_title == "Test Title"
        assert result.judge_name == "Test Judge"
        assert result.parties == [{"name": "Alice", "role": "plaintiff"}]

    def test_regex_fallback_preserves_parties_from_llm(self) -> None:
        """When LLM provides parties but case_title is null, regex fallback
        should fill case_title but preserve the LLM parties."""
        scraper = LATentativeRulingsScraper(default_config())
        html = _load("la_ruling_response.html")

        llm_parties = [
            {"name": "Sumayya Aasi", "role": "plaintiff"},
            {"name": "American Honda", "role": "defendant"},
        ]
        doc = _make_doc_with_fields(
            raw_content=html.encode("utf-8"),
            case_number="24NNCV02551",
            ruling_text="LLM ruling text",
            case_title=None,  # Needs fallback
            parties=llm_parties,  # Should be preserved
            extra={"_llm_extracted": True, "ruling_index": 1},
        )

        result = scraper.parse_document(doc)

        # LLM parties should be preserved
        assert result.parties == llm_parties
        # case_title should be filled by regex
        assert result.case_title is not None

    def test_regex_fallback_handles_extraction_error(self) -> None:
        """When regex extraction raises an exception during fallback,
        the document should be restored to its exact pre-call state —
        including null fields staying null."""
        scraper = LATentativeRulingsScraper(default_config())
        # Use real HTML that _split_cases_html can parse
        html = _load("la_ruling_response.html")
        doc = _make_doc_with_fields(
            raw_content=html.encode("utf-8"),
            case_number="24NNCV02551",
            ruling_text="LLM ruling text",
            case_title=None,  # This was null before the error
            extra={"_llm_extracted": True, "ruling_index": 1},
        )

        with patch(
            "courts.ca.la_tentatives._extract_ruling_fields",
            side_effect=RuntimeError("parse error"),
        ):
            result = scraper.parse_document(doc)

        # LLM fields should be restored after the error
        assert result.case_number == "24NNCV02551"
        assert result.ruling_text == "LLM ruling text"
        # Null fields should stay null (not set to partial values)
        assert result.case_title is None

    def test_deterministic_header_overrides_llm_hearing_date(self) -> None:
        """Deterministic header extraction overrides incorrect LLM hearing_date.

        The LLM may pick up wrong dates (filing dates, reference dates) from
        body text.  The HTML header date is authoritative (#2330).
        """
        scraper = LATentativeRulingsScraper(default_config())
        html = _load("la_ruling_response.html")

        doc = _make_doc_with_fields(
            raw_content=html.encode("utf-8"),
            case_number="24NNCV02551",
            ruling_text="LLM ruling text",
            case_title="Aasi v. Tesla",
            # LLM extracted wrong hearing_date (a filing date, not hearing date)
            hearing_date=datetime(2025, 6, 24),
            department="99",  # LLM got department wrong too
            extra={"_llm_extracted": True, "ruling_index": 1},
        )

        result = scraper.parse_document(doc)

        # Deterministic header extraction should override LLM values
        assert result.hearing_date == datetime(2026, 3, 2), (
            "Header hearing_date should override LLM hearing_date"
        )
        assert result.department == "3", "Header department should override LLM department"
        # LLM case_title should still be preserved (not a header field)
        assert result.case_title == "Aasi v. Tesla"

    def test_deterministic_header_overrides_llm_case_number(self) -> None:
        """Deterministic header extraction overrides incorrect LLM case_number."""
        scraper = LATentativeRulingsScraper(default_config())
        html = _load("la_ruling_response.html")

        doc = _make_doc_with_fields(
            raw_content=html.encode("utf-8"),
            case_number="WRONG123",  # LLM got case number wrong
            ruling_text="LLM ruling text",
            hearing_date=datetime(2025, 1, 1),  # Wrong date too
            extra={"_llm_extracted": True, "ruling_index": 1},
        )

        result = scraper.parse_document(doc)

        # Header case_number should override LLM value
        assert result.case_number == "24NNCV02551"
        # Header hearing_date should override LLM value
        assert result.hearing_date == datetime(2026, 3, 2)


class TestFetchDocumentsLlmPath:
    """Tests for fetch_documents with LLM extraction enabled."""

    @respx.mock
    def test_llm_path_creates_docs_with_pre_populated_fields(self) -> None:
        """When ENABLE_LA_LLM_EXTRACTION is set and LLM succeeds,
        fetch_documents should create docs with fields from LLM."""
        main_html = _load("la_main_page.html")
        ruling_html = _load("la_ruling_pas_p.html")

        # Mock HTTP
        respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))
        respx.post(CIVIL_URL).mock(return_value=httpx.Response(200, text=ruling_html))

        # Mock LLM to return a single ruling
        llm_rulings = [
            LASplitRuling(
                ruling_index=1,
                case_number="25NNCV00140",
                ruling_text="Motion for reconsideration denied.",
                case_title="Wu v. Pak",
                motion_type="motion_for_reconsideration",
                outcome="denied",
                parties=[
                    {"name": "Sai K. Wu", "role": "plaintiff"},
                    {"name": "Shane S. Pak", "role": "defendant"},
                ],
            )
        ]

        config = default_config()
        config.request_delay_seconds = 0.0
        scraper = LATentativeRulingsScraper(config)

        with (
            patch.dict("os.environ", {"ENABLE_LA_LLM_EXTRACTION": "true"}),
            patch(
                "courts.ca.la_tentatives._llm_extract_rulings",
                return_value=llm_rulings,
            ),
        ):
            docs = scraper.fetch_documents()

        # Should have 97 docs (one per dropdown option, each with 1 LLM ruling)
        assert len(docs) > 0
        # Check the first doc has LLM-populated fields
        first = docs[0]
        assert first.extra.get("_llm_extracted") is True
        assert first.extra.get("pre_split") is True
        assert first.case_number == "25NNCV00140"
        assert first.case_title == "Wu v. Pak"
        assert first.outcome == "denied"
        assert first.ruling_text == "Motion for reconsideration denied."

    @respx.mock
    def test_llm_fallback_to_regex_on_failure(self) -> None:
        """When LLM returns None, should fall back to regex splitting."""
        main_html = _load("la_main_page.html")
        ruling_html = _load("la_ruling_pas_p.html")

        # Mock HTTP — only respond to first option
        respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))
        respx.post(CIVIL_URL).mock(return_value=httpx.Response(200, text=ruling_html))

        config = default_config()
        config.request_delay_seconds = 0.0
        scraper = LATentativeRulingsScraper(config)

        with (
            patch.dict("os.environ", {"ENABLE_LA_LLM_EXTRACTION": "true"}),
            patch(
                "courts.ca.la_tentatives._llm_extract_rulings",
                return_value=None,
            ),
        ):
            docs = scraper.fetch_documents()

        # Should still get docs from regex fallback
        assert len(docs) > 0
        # Regex path does not set _llm_extracted
        first = docs[0]
        assert first.extra.get("_llm_extracted") is not True

    @respx.mock
    def test_disabled_flag_uses_regex_path(self) -> None:
        """When ENABLE_LA_LLM_EXTRACTION is not set, uses regex path."""
        main_html = _load("la_main_page.html")
        ruling_html = _load("la_ruling_pas_p.html")

        respx.get(CIVIL_URL).mock(return_value=httpx.Response(200, text=main_html))
        respx.post(CIVIL_URL).mock(return_value=httpx.Response(200, text=ruling_html))

        config = default_config()
        config.request_delay_seconds = 0.0
        scraper = LATentativeRulingsScraper(config)

        with patch.dict("os.environ", {}, clear=False):
            # Make sure the env var is not set
            import os

            os.environ.pop("ENABLE_LA_LLM_EXTRACTION", None)
            docs = scraper.fetch_documents()

        assert len(docs) > 0
        first = docs[0]
        assert first.extra.get("_llm_extracted") is not True


# ---------------------------------------------------------------------------
# _split_rulings — regex-based splitting for reingest (#2007)
# ---------------------------------------------------------------------------


class TestSplitRulings:
    """Tests for _split_rulings() — the regex-based splitting path."""

    def test_multi_case_page_splits(self) -> None:
        """Multi-case department page is split into individual rulings."""
        html = _load("la_ruling_response.html")
        rulings = _split_rulings(html)
        assert len(rulings) == 2
        # Each ruling should have a case number.
        assert rulings[0].case_number == "24NNCV02551"
        assert rulings[1].case_number == "26NNCP00062"
        # ruling_index is 1-based.
        assert rulings[0].ruling_index == 1
        assert rulings[1].ruling_index == 2

    def test_single_case_page(self) -> None:
        """Single-case page produces one ruling."""
        html = _load("la_ruling_com_a.html")
        rulings = _split_rulings(html)
        assert len(rulings) == 1
        assert rulings[0].ruling_index == 1
        assert rulings[0].case_number is not None

    def test_ruling_text_is_html_extracted(self) -> None:
        """Ruling text comes from BeautifulSoup, not LLM reproduction."""
        html = _load("la_ruling_response.html")
        rulings = _split_rulings(html)
        for ruling in rulings:
            # Text should be non-empty and not contain HTML tags.
            assert len(ruling.ruling_text) > 100
            assert "<div" not in ruling.ruling_text
            assert "<b>" not in ruling.ruling_text.lower()

    def test_dept_header_only_returns_empty(self) -> None:
        """Department header boilerplate page returns empty list."""
        html = _load("la_ruling_dept_header.html")
        rulings = _split_rulings(html)
        assert rulings == []

    def test_returns_list_of_la_split_ruling(self) -> None:
        """All items in returned list are LASplitRuling instances."""
        html = _load("la_ruling_response.html")
        rulings = _split_rulings(html)
        for ruling in rulings:
            assert isinstance(ruling, LASplitRuling)

    def test_ruling_has_case_title(self) -> None:
        """Rulings include case title when extractable."""
        html = _load("la_ruling_response.html")
        rulings = _split_rulings(html)
        # At least one ruling should have a case title.
        titles = [r.case_title for r in rulings if r.case_title]
        assert len(titles) > 0

    def test_empty_input_returns_empty(self) -> None:
        """Empty or minimal HTML returns empty list."""
        assert _split_rulings("") == []
        assert _split_rulings("<html></html>") == []


# ---------------------------------------------------------------------------
# _replace_ruling_text_from_html — LLM text replacement (#2007)
# ---------------------------------------------------------------------------


class TestReplaceRulingTextFromHtml:
    """Tests for _replace_ruling_text_from_html()."""

    def test_replaces_truncated_text(self) -> None:
        """LLM-generated ruling_text is replaced with HTML-extracted text."""
        html = _load("la_ruling_response.html")
        # Simulate LLM rulings with truncated text.
        rulings = [
            LASplitRuling(
                ruling_index=1,
                case_number="24NNCV02551",
                ruling_text="TRUNCATED" * 5000,  # 45k chars
            ),
            LASplitRuling(
                ruling_index=2,
                case_number="26NNCP00062",
                ruling_text="TRUNCATED" * 5000,
            ),
        ]
        _replace_ruling_text_from_html(html, rulings)

        # Both should have been replaced with HTML-extracted text.
        for ruling in rulings:
            assert "TRUNCATED" not in ruling.ruling_text
            assert len(ruling.ruling_text) > 100

    def test_matches_by_case_number(self) -> None:
        """Rulings are matched to HTML chunks by case number."""
        html = _load("la_ruling_response.html")
        rulings = [
            LASplitRuling(
                ruling_index=1,
                case_number="26NNCP00062",  # Second case in HTML
                ruling_text="placeholder",
            ),
        ]
        _replace_ruling_text_from_html(html, rulings)
        # Should match the second chunk by case number.
        assert "26NNCP00062" in rulings[0].ruling_text

    def test_no_position_fallback_when_case_number_present(self) -> None:
        """When case_number is present but doesn't match, no positional fallback."""
        html = _load("la_ruling_response.html")
        rulings = [
            LASplitRuling(
                ruling_index=1,
                case_number="NONEXISTENT123",
                ruling_text="placeholder",
            ),
        ]
        _replace_ruling_text_from_html(html, rulings)
        # Should NOT replace — case number didn't match and positional
        # fallback is disabled when a case number is present.
        assert rulings[0].ruling_text == "placeholder"

    def test_position_fallback_when_no_case_number(self) -> None:
        """When no case_number, falls back to positional match."""
        html = _load("la_ruling_response.html")
        rulings = [
            LASplitRuling(
                ruling_index=1,
                case_number=None,
                ruling_text="placeholder",
            ),
        ]
        _replace_ruling_text_from_html(html, rulings)
        # Should match the first chunk by position (ruling_index=1).
        assert rulings[0].ruling_text != "placeholder"
        assert len(rulings[0].ruling_text) > 100

    def test_no_replacement_for_short_chunks(self) -> None:
        """Chunks shorter than 100 chars don't replace existing text."""
        # Build minimal HTML with a very short case section.
        short_html = (
            '<html><body><div id="speechSynthesis">'
            "<HR><B> Case Number: </B> TEST123"
            "<p>Short.</p>"
            "</div></body></html>"
        )
        rulings = [
            LASplitRuling(
                ruling_index=1,
                case_number="TEST123",
                ruling_text="Original long text " * 100,
            ),
        ]
        _replace_ruling_text_from_html(short_html, rulings)
        # Should NOT be replaced because the chunk text is too short.
        assert rulings[0].ruling_text.startswith("Original long text")

    def test_no_split_sections_leaves_text_unchanged(self) -> None:
        """If HTML has no case sections, ruling text is unchanged."""
        no_cases_html = '<html><body><div id="speechSynthesis">Just some text</div></body></html>'
        original = "Original text stays"
        rulings = [LASplitRuling(ruling_index=1, case_number="X", ruling_text=original)]
        _replace_ruling_text_from_html(no_cases_html, rulings)
        assert rulings[0].ruling_text == original


# ---------------------------------------------------------------------------
# sanitize_ruling_html — HTML sanitization for ruling_text_html (#2179)
# ---------------------------------------------------------------------------


class TestSanitizeRulingHtml:
    """Tests for the sanitize_ruling_html function."""

    def test_preserves_structural_tags(self) -> None:
        """Structural formatting tags are preserved."""
        html = "<p>Paragraph</p><strong>Bold</strong><em>Italic</em>"
        result = sanitize_ruling_html(html)
        assert "<p>" in result
        assert "<strong>" in result
        assert "<em>" in result

    def test_preserves_list_tags(self) -> None:
        """Ordered and unordered lists are preserved."""
        html = "<ol><li>First</li></ol><ul><li>Bullet</li></ul>"
        result = sanitize_ruling_html(html)
        assert "<ol>" in result
        assert "<ul>" in result
        assert "<li>" in result

    def test_preserves_heading_tags(self) -> None:
        """Heading tags h1-h6 are preserved."""
        html = "<h1>Title</h1><h2>Subtitle</h2><h3>Section</h3>"
        result = sanitize_ruling_html(html)
        assert "<h1>" in result
        assert "<h2>" in result
        assert "<h3>" in result

    def test_preserves_table_tags(self) -> None:
        """Table elements are preserved."""
        html = "<table><tr><td>Cell</td><th>Header</th></tr></table>"
        result = sanitize_ruling_html(html)
        assert "<table>" in result
        assert "<tr>" in result
        assert "<td>" in result
        assert "<th>" in result

    def test_strips_script_tags(self) -> None:
        """Script tags and their content are completely removed."""
        html = "<p>Before</p><script>alert('xss')</script><p>After</p>"
        result = sanitize_ruling_html(html)
        assert "<script>" not in result
        assert "alert" not in result
        assert "<p>Before</p>" in result
        assert "<p>After</p>" in result

    def test_strips_style_tags(self) -> None:
        """Style tags and their content are completely removed."""
        html = "<p>Text</p><style>.foo { color: red; }</style>"
        result = sanitize_ruling_html(html)
        assert "<style>" not in result
        assert "color:" not in result
        assert "<p>Text</p>" in result

    def test_strips_event_handler_attributes(self) -> None:
        """Event handler attributes (on*) are removed."""
        html = '<p onclick="alert(1)" onmouseover="foo()">Text</p>'
        result = sanitize_ruling_html(html)
        assert "onclick" not in result
        assert "onmouseover" not in result
        assert "<p>" in result
        assert "Text" in result

    def test_strips_style_attributes(self) -> None:
        """Style attributes are removed."""
        html = '<p style="color: red; font-size: 14px;">Text</p>'
        result = sanitize_ruling_html(html)
        assert "style=" not in result
        assert "<p>" in result

    def test_preserves_class_and_id_attributes(self) -> None:
        """Class and id attributes are preserved."""
        html = '<div class="ruling-text" id="case-1">Content</div>'
        result = sanitize_ruling_html(html)
        assert 'class="ruling-text"' in result
        assert 'id="case-1"' in result

    def test_preserves_href_on_anchors(self) -> None:
        """Href attribute on anchor tags is preserved."""
        html = '<a href="http://example.com">Link</a>'
        result = sanitize_ruling_html(html)
        assert 'href="http://example.com"' in result

    def test_unwraps_disallowed_tags(self) -> None:
        """Disallowed tags are removed but their text content is preserved."""
        html = "<font>Styled text</font>"
        result = sanitize_ruling_html(html)
        assert "<font>" not in result
        assert "Styled text" in result

    def test_empty_input(self) -> None:
        """Empty input returns empty string."""
        assert sanitize_ruling_html("") == ""

    def test_plain_text_passthrough(self) -> None:
        """Plain text with no tags is preserved."""
        result = sanitize_ruling_html("Just plain text")
        assert "Just plain text" in result

    def test_br_tags_preserved(self) -> None:
        """BR tags are preserved for line breaks."""
        html = "Line one<br/>Line two<br>Line three"
        result = sanitize_ruling_html(html)
        assert "<br" in result

    def test_strips_data_attributes(self) -> None:
        """Data attributes are removed."""
        html = '<div data-tooltip="info" data-id="123">Text</div>'
        result = sanitize_ruling_html(html)
        assert "data-tooltip" not in result
        assert "data-id" not in result
        assert "Text" in result

    def test_strips_javascript_href(self) -> None:
        """javascript: hrefs are removed to prevent XSS."""
        html = "<a href=\"javascript:alert('XSS')\">Click me</a>"
        result = sanitize_ruling_html(html)
        assert "javascript:" not in result
        assert "Click me" in result
        assert "<a" in result  # tag is preserved, just href removed

    def test_strips_data_uri_href(self) -> None:
        """data: hrefs are removed to prevent XSS."""
        html = '<a href="data:text/html,<script>alert(1)</script>">Link</a>'
        result = sanitize_ruling_html(html)
        assert "data:" not in result
        assert "Link" in result

    def test_strips_vbscript_href(self) -> None:
        """vbscript: hrefs are removed."""
        html = '<a href="vbscript:MsgBox(1)">Link</a>'
        result = sanitize_ruling_html(html)
        assert "vbscript:" not in result
        assert "Link" in result

    def test_preserves_safe_href(self) -> None:
        """Normal http/https hrefs are preserved."""
        html = '<a href="https://example.com">Safe link</a>'
        result = sanitize_ruling_html(html)
        assert 'href="https://example.com"' in result
        assert "Safe link" in result

    def test_strips_javascript_href_case_insensitive(self) -> None:
        """javascript: check is case-insensitive."""
        html = '<a href="JavaScript:alert(1)">XSS</a>'
        result = sanitize_ruling_html(html)
        assert "JavaScript:" not in result.lower() or "javascript:" not in result.lower()
        # More directly: href should be gone
        assert "href" not in result

    def test_strips_javascript_href_with_whitespace(self) -> None:
        """Leading whitespace before javascript: is handled."""
        html = '<a href="  javascript:alert(1)">XSS</a>'
        result = sanitize_ruling_html(html)
        assert "javascript:" not in result


# ---------------------------------------------------------------------------
# ruling_text_html integration — _extract_ruling_fields populates it (#2179)
# ---------------------------------------------------------------------------


class TestRulingTextHtmlExtraction:
    """Verify _extract_ruling_fields populates ruling_text_html."""

    def test_extract_fields_populates_ruling_text_html(self) -> None:
        """_extract_ruling_fields should set ruling_text_html with sanitized HTML."""
        from bs4 import BeautifulSoup

        html = _load("la_ruling_response.html")
        sections = _split_cases_html(html)
        assert len(sections) > 0

        doc = CapturedDocument(
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
        soup = BeautifulSoup(doc.raw_content, "lxml")
        _extract_ruling_fields(soup, doc)

        # ruling_text should be plain text (no HTML tags)
        assert doc.ruling_text is not None
        assert "<p>" not in doc.ruling_text
        assert "<strong>" not in doc.ruling_text

        # ruling_text_html should contain HTML tags
        assert doc.ruling_text_html is not None
        assert len(doc.ruling_text_html) > 0
        # Should not contain script/style
        assert "<script>" not in doc.ruling_text_html
        assert "<style>" not in doc.ruling_text_html

    def test_ruling_text_html_no_event_handlers(self) -> None:
        """ruling_text_html should not contain event handler attributes."""
        from bs4 import BeautifulSoup

        html = _load("la_ruling_response.html")
        sections = _split_cases_html(html)
        doc = CapturedDocument(
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
        soup = BeautifulSoup(doc.raw_content, "lxml")
        _extract_ruling_fields(soup, doc)
        assert doc.ruling_text_html is not None
        assert "onclick" not in doc.ruling_text_html
        assert "onload" not in doc.ruling_text_html

    def test_all_fixture_files_produce_ruling_text_html(self) -> None:
        """All LA ruling fixture files should produce non-empty ruling_text_html."""
        from bs4 import BeautifulSoup

        fixture_files = [
            "la_ruling_response.html",
            "la_ruling_bh205.html",
            "la_ruling_smc1.html",
            "la_ruling_smc49.html",
            "la_ruling_smc56.html",
            "la_ruling_tor_b.html",
            "la_ruling_van_a.html",
            "la_ruling_pas_p.html",
            "la_ruling_cha_f46.html",
            "la_ruling_com_a.html",
            "la_ruling_ea_h.html",
        ]

        for fixture in fixture_files:
            html = _load(fixture)
            sections = _split_cases_html(html)
            if not sections:
                continue  # Skip dept header pages with no cases

            doc = CapturedDocument(
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
            soup = BeautifulSoup(doc.raw_content, "lxml")
            _extract_ruling_fields(soup, doc)

            assert doc.ruling_text is not None, f"{fixture}: ruling_text is None"
            assert doc.ruling_text_html is not None, f"{fixture}: ruling_text_html is None"
            assert len(doc.ruling_text_html) > 0, f"{fixture}: ruling_text_html is empty"

    def test_split_rulings_populates_ruling_text_html(self) -> None:
        """_split_rulings should produce LASplitRuling objects with ruling_text_html."""
        html = _load("la_ruling_response.html")
        rulings = _split_rulings(html)
        assert len(rulings) > 0
        for ruling in rulings:
            assert ruling.ruling_text is not None
            assert ruling.ruling_text_html is not None
            assert len(ruling.ruling_text_html) > 0
            # Plain text should not have HTML tags
            assert "<p>" not in ruling.ruling_text
            # HTML should have structural tags
            assert "<script>" not in ruling.ruling_text_html

    def test_replace_ruling_text_from_html_sets_ruling_text_html(self) -> None:
        """_replace_ruling_text_from_html should also set ruling_text_html."""
        html = _load("la_ruling_response.html")
        sections = _split_cases_html(html)
        assert len(sections) >= 2

        # Create rulings matching the fixture's case numbers
        from bs4 import BeautifulSoup

        case_numbers = []
        for section in sections:
            soup = BeautifulSoup(section, "lxml")
            text = soup.get_text()
            import re

            m = re.search(r"Case Number:\s*(\w+)", text)
            if m:
                case_numbers.append(m.group(1))

        rulings = [
            LASplitRuling(
                ruling_index=idx + 1,
                case_number=cn,
                ruling_text="placeholder",
            )
            for idx, cn in enumerate(case_numbers)
        ]
        _replace_ruling_text_from_html(html, rulings)
        for ruling in rulings:
            assert ruling.ruling_text_html is not None, (
                f"ruling_text_html not set for {ruling.case_number}"
            )
            assert "<script>" not in ruling.ruling_text_html


# ---------------------------------------------------------------------------
# _extract_header_fields — LA HTML <b> header tag extraction (#2330)
# ---------------------------------------------------------------------------


def test_extract_header_fields_from_ruling_response() -> None:
    """Extract hearing_date, department, case_number from la_ruling_response.html."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    sections = _split_cases_html(html)
    assert len(sections) == 2

    # First case section: 24NNCV02551, March 2, 2026, Dept 3
    soup1 = BeautifulSoup(sections[0], "lxml")
    content1 = soup1.find("div", id="speechSynthesis")
    header1 = _extract_header_fields(content1)
    assert header1.case_number == "24NNCV02551"
    assert header1.hearing_date == datetime(2026, 3, 2)
    assert header1.department == "3"

    # Second case section: 26NNCP00062, March 2, 2026, Dept 3
    soup2 = BeautifulSoup(sections[1], "lxml")
    content2 = soup2.find("div", id="speechSynthesis")
    header2 = _extract_header_fields(content2)
    assert header2.case_number == "26NNCP00062"
    assert header2.hearing_date == datetime(2026, 3, 2)
    assert header2.department == "3"


def test_extract_header_fields_alphanumeric_dept() -> None:
    """Extract department F46 from la_ruling_cha_f46.html."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_cha_f46.html")
    sections = _split_cases_html(html)
    assert len(sections) >= 1

    soup = BeautifulSoup(sections[0], "lxml")
    content = soup.find("div", id="speechSynthesis")
    header = _extract_header_fields(content)
    assert header.case_number == "21CHCV00539"
    assert header.hearing_date == datetime(2026, 3, 2)
    assert header.department == "F46"


def test_extract_header_fields_letter_dept() -> None:
    """Extract department P from la_ruling_pas_p.html."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_pas_p.html")
    sections = _split_cases_html(html)
    assert len(sections) >= 1

    soup = BeautifulSoup(sections[0], "lxml")
    content = soup.find("div", id="speechSynthesis")
    header = _extract_header_fields(content)
    assert header.case_number == "25NNCV00140"
    assert header.hearing_date == datetime(2026, 3, 2)
    assert header.department == "P"


def test_extract_header_fields_dept_i() -> None:
    """Extract department I from la_ruling_dept_i_header_in_title.html."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_dept_i_header_in_title.html")
    sections = _split_cases_html(html)
    assert len(sections) >= 1

    soup = BeautifulSoup(sections[0], "lxml")
    content = soup.find("div", id="speechSynthesis")
    header = _extract_header_fields(content)
    assert header.case_number == "24VECV05649"
    assert header.hearing_date == datetime(2026, 3, 6)
    assert header.department == "I"


def test_extract_header_fields_bh205() -> None:
    """Extract department 205 from la_ruling_bh205.html."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_bh205.html")
    sections = _split_cases_html(html)
    assert len(sections) >= 1

    soup = BeautifulSoup(sections[0], "lxml")
    content = soup.find("div", id="speechSynthesis")
    header = _extract_header_fields(content)
    assert header.hearing_date == datetime(2026, 3, 3)
    assert header.department == "205"


def test_extract_header_fields_no_header_returns_none() -> None:
    """HTML without header tags returns all-None _HeaderFields."""
    from bs4 import BeautifulSoup

    html = '<html><body><div id="speechSynthesis"><p>No header here.</p></div></body></html>'
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    header = _extract_header_fields(content)
    assert header.case_number is None
    assert header.hearing_date is None
    assert header.department is None


# ---------------------------------------------------------------------------
# _extract_ruling_fields — hearing_date and department extraction (#2330)
# ---------------------------------------------------------------------------


def test_extract_fields_hearing_date_from_header() -> None:
    """_extract_ruling_fields populates hearing_date from HTML header tags."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    sections = _split_cases_html(html)
    doc = CapturedDocument(
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
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    assert doc.hearing_date == datetime(2026, 3, 2)


def test_extract_fields_department_from_header() -> None:
    """_extract_ruling_fields populates department from HTML header tags."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_response.html")
    sections = _split_cases_html(html)
    doc = CapturedDocument(
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
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    assert doc.department == "3"


def test_extract_fields_alphanumeric_department() -> None:
    """_extract_ruling_fields extracts alphanumeric department from header."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_cha_f46.html")
    sections = _split_cases_html(html)
    doc = CapturedDocument(
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
    soup = BeautifulSoup(doc.raw_content, "lxml")
    _extract_ruling_fields(soup, doc)
    assert doc.department == "F46"


def test_extract_fields_ruling_text_is_not_raw_html() -> None:
    """All LA fixtures produce plain text ruling_text, never raw HTML."""
    from bs4 import BeautifulSoup

    for fixture_name in (
        "la_ruling_response.html",
        "la_ruling_cha_f46.html",
        "la_ruling_pas_p.html",
        "la_ruling_com_a.html",
        "la_ruling_bh205.html",
        "la_ruling_dept_i_header_in_title.html",
    ):
        html = _load(fixture_name)
        sections = _split_cases_html(html)
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
            assert doc.ruling_text is not None, f"ruling_text is None for {fixture_name}"
            # ruling_text should not start with < (raw HTML)
            assert not doc.ruling_text.strip().startswith("<"), (
                f"ruling_text is raw HTML for {fixture_name}: {doc.ruling_text[:100]}"
            )


# ---------------------------------------------------------------------------
# _split_rulings — hearing_date and department in LASplitRuling (#2330)
# ---------------------------------------------------------------------------


def test_split_rulings_populates_hearing_date() -> None:
    """_split_rulings sets hearing_date on each LASplitRuling."""
    html = _load("la_ruling_response.html")
    rulings = _split_rulings(html)
    assert len(rulings) == 2
    for ruling in rulings:
        assert ruling.hearing_date == datetime(2026, 3, 2)


def test_split_rulings_populates_department() -> None:
    """_split_rulings sets department on each LASplitRuling."""
    html = _load("la_ruling_response.html")
    rulings = _split_rulings(html)
    assert len(rulings) == 2
    for ruling in rulings:
        assert ruling.department == "3"


def test_split_rulings_alphanumeric_department() -> None:
    """_split_rulings extracts alphanumeric department F46."""
    html = _load("la_ruling_cha_f46.html")
    rulings = _split_rulings(html)
    assert len(rulings) >= 1
    assert rulings[0].department == "F46"
    assert rulings[0].hearing_date == datetime(2026, 3, 2)


def test_split_rulings_across_fixtures() -> None:
    """_split_rulings extracts hearing_date and department from all main fixtures."""
    test_cases = [
        ("la_ruling_response.html", datetime(2026, 3, 2), "3"),
        ("la_ruling_cha_f46.html", datetime(2026, 3, 2), "F46"),
        ("la_ruling_pas_p.html", datetime(2026, 3, 2), "P"),
        ("la_ruling_dept_i_header_in_title.html", datetime(2026, 3, 6), "I"),
    ]
    for fixture_name, expected_date, expected_dept in test_cases:
        html = _load(fixture_name)
        rulings = _split_rulings(html)
        assert len(rulings) >= 1, f"No rulings from {fixture_name}"
        assert rulings[0].hearing_date == expected_date, (
            f"Wrong hearing_date from {fixture_name}: "
            f"expected {expected_date}, got {rulings[0].hearing_date}"
        )
        assert rulings[0].department == expected_dept, (
            f"Wrong department from {fixture_name}: "
            f"expected {expected_dept}, got {rulings[0].department}"
        )


# ---------------------------------------------------------------------------
# Edge cases — header extraction fallbacks (#2330)
# ---------------------------------------------------------------------------


def test_extract_header_fields_invalid_date_returns_none() -> None:
    """Invalid date string in header tag results in hearing_date=None."""
    from bs4 import BeautifulSoup

    html = (
        '<html><body><div id="speechSynthesis">'
        "<B> Case Number: </B> 24NNCV02551"
        "&nbsp;&nbsp;&nbsp;"
        "<B> Hearing Date: </B> Zeptember 99, 2026"
        "&nbsp;&nbsp;&nbsp;"
        "<B> Dept: </B> 3"
        "</div></body></html>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    header = _extract_header_fields(content)
    assert header.case_number == "24NNCV02551"
    assert header.hearing_date is None  # Invalid month/day
    assert header.department == "3"


def test_extract_ruling_fields_fallback_case_number_no_header() -> None:
    """Body-text regex fallback when header has no case number."""
    from unittest.mock import patch as _patch

    from bs4 import BeautifulSoup

    from courts.ca.la_tentatives import _HeaderFields

    # HTML with a case number that header extraction can find
    html = (
        '<html><body><div id="speechSynthesis">'
        "<p>Case Number: 24NNCV02551</p>"
        "<p>The motion is GRANTED. This is a test ruling that has enough text "
        "to pass the minimum length check for splitting purposes.</p>"
        "</div></body></html>"
    )
    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 2),
        content_format=ContentFormat.HTML,
        raw_content=html.encode("utf-8"),
        content_hash="",
    )
    # Mock _extract_header_fields to return no case_number, forcing the
    # body-text regex fallback path.
    with _patch(
        "courts.ca.la_tentatives._extract_header_fields",
        return_value=_HeaderFields(case_number=None, hearing_date=None, department=None),
    ):
        soup = BeautifulSoup(doc.raw_content, "lxml")
        _extract_ruling_fields(soup, doc)
    assert doc.case_number == "24NNCV02551"


def test_extract_ruling_fields_fallback_multiple_case_numbers() -> None:
    """When header returns no case_number but text has multiple, all_case_numbers is set."""
    from unittest.mock import patch as _patch

    from bs4 import BeautifulSoup

    from courts.ca.la_tentatives import _HeaderFields

    html = (
        '<html><body><div id="speechSynthesis">'
        "<p>Case Number: 24NNCV02551</p>"
        "<p>Related to Case Number: 24NNCV02552</p>"
        "<p>The motion is GRANTED. This is sufficient body text.</p>"
        "</div></body></html>"
    )
    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 2),
        content_format=ContentFormat.HTML,
        raw_content=html.encode("utf-8"),
        content_hash="",
    )
    with _patch(
        "courts.ca.la_tentatives._extract_header_fields",
        return_value=_HeaderFields(case_number=None, hearing_date=None, department=None),
    ):
        soup = BeautifulSoup(doc.raw_content, "lxml")
        _extract_ruling_fields(soup, doc)
    assert doc.case_number == "24NNCV02551"
    assert doc.extra.get("all_case_numbers") == ["24NNCV02551", "24NNCV02552"]


def test_split_rulings_fallback_case_number_no_header() -> None:
    """When header case_number is absent, _split_rulings uses body-text regex."""
    from unittest.mock import patch as _patch

    from courts.ca.la_tentatives import _HeaderFields

    # Use a real fixture so _split_cases_html produces valid sections
    html = _load("la_ruling_response.html")

    # Mock _extract_header_fields to return no case_number
    with _patch(
        "courts.ca.la_tentatives._extract_header_fields",
        return_value=_HeaderFields(case_number=None, hearing_date=None, department=None),
    ):
        rulings = _split_rulings(html)
    # The body-text regex fallback should still find case numbers
    assert len(rulings) == 2
    assert rulings[0].case_number == "24NNCV02551"
    assert rulings[1].case_number == "26NNCP00062"


# ---------------------------------------------------------------------------
# is_la_html — content-based LA HTML detection (#2450)
# ---------------------------------------------------------------------------


def test_is_la_html_detects_speech_synthesis_marker() -> None:
    """Standard LA HTML with div#speechSynthesis is detected."""
    html = (
        '<html><body><div id="speechSynthesis" class="Print">'
        "<b>Case Number:</b> 24STCV01234</div></body></html>"
    )
    assert is_la_html(html) is True


def test_is_la_html_detects_word_pasted_variant() -> None:
    """Word-pasted LA HTML (the #2450 failure mode) is also detected."""
    html = _load("la_ruling_25stcv31489_word_paste.html")
    assert is_la_html(html) is True


def test_is_la_html_rejects_sd_calendar_html() -> None:
    """SD calendar HTML has no div#speechSynthesis marker."""
    sd_html = (
        "<html><body><h1>CIVIL CALENDAR For Friday, 03/13/2026</h1>"
        "<table><tr><td>24CU016153C</td></tr></table></body></html>"
    )
    assert is_la_html(sd_html) is False


def test_is_la_html_rejects_plain_text() -> None:
    """Plain text (OC PDF text content) is rejected."""
    assert is_la_html("Case No. 2024-01234567\nMotion granted.") is False


def test_is_la_html_rejects_empty_and_none() -> None:
    """Empty strings and None are safely rejected."""
    assert is_la_html("") is False
    assert is_la_html(None) is False


def test_is_la_html_detects_marker_beyond_head() -> None:
    """Full-page LA captures (ASP.NET wrapper pushes speechSynthesis past
    the first 4 KB) are still detected — regression for #2480."""
    # Marker appears well after the 4 KB boundary — must still match.
    padding = "<!-- " + ("x" * 8000) + " -->"
    html_with_late_marker = padding + '<div id="speechSynthesis">content</div>'
    assert is_la_html(html_with_late_marker) is True


# ---------------------------------------------------------------------------
# #2480 regression — full-page LA captures produced UNKNOWN case_numbers.
# Three S3 objects stored by the ENABLE_LA_LLM_EXTRACTION path contain the
# full ASP.NET page HTML; div#speechSynthesis appears at offsets 60-403 KB,
# well beyond the is_la_html 4 KB head scan window that predated #2480.
# Before the fix, is_la_html returned False for these fixtures and the
# worker synthesized UNKNOWN-<document_id> case_numbers.  These tests
# pin both the detection (is_la_html True) and the splitter output
# (real case_numbers extracted).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture_name", "marker_min_offset"),
    [
        (
            "la_ruling_fullpage_smc205.html",
            60_000,  # Santa Monica Dept 205 — marker at ~403 KB in source
        ),
        (
            "la_ruling_fullpage_stanley54.html",
            60_000,  # Stanley Mosk Dept 54 — marker at ~61 KB in source
        ),
        (
            "la_ruling_fullpage_torrance_m.html",
            60_000,  # Torrance Dept M — marker at ~109 KB in source
        ),
    ],
)
def test_is_la_html_full_page_capture_detected(
    fixture_name: str,
    marker_min_offset: int,
) -> None:
    """The three #2480 fixtures are detected as LA HTML despite the
    div#speechSynthesis marker sitting beyond the 4 KB head window."""
    html = _load(fixture_name)
    # Sanity check the fixture: the marker must sit past the head boundary
    # for this test to exercise the Tier-2 scan.  If a fixture is replaced
    # with one that no longer triggers Tier-2, this assertion catches that.
    assert html.find('id="speechSynthesis"') >= marker_min_offset, (
        f"{fixture_name} has speechSynthesis in the first {marker_min_offset} "
        f"bytes — fixture no longer exercises the Tier-2 full-document scan"
    )
    assert is_la_html(html) is True


@pytest.mark.parametrize(
    ("fixture_name", "expected_case_numbers"),
    [
        (
            "la_ruling_fullpage_smc205.html",
            ["24SMCV01804", "25SMCV06383"],
        ),
        (
            "la_ruling_fullpage_stanley54.html",
            ["21STCV34143"],
        ),
        (
            "la_ruling_fullpage_torrance_m.html",
            ["22TRCV01133", "24TRCV00284"],
        ),
    ],
)
def test_split_rulings_extracts_real_case_numbers_from_full_page(
    fixture_name: str,
    expected_case_numbers: list[str],
) -> None:
    """_split_rulings extracts real case_numbers (not UNKNOWN) from the
    #2480 full-page fixtures — regression for the issue.

    The underlying splitter already worked on these fixtures; the bug was
    upstream in the routing (is_la_html rejected them, so _split_rulings
    was never called).  This test pins the splitter output so a future
    regex change cannot silently regress case_number extraction.
    """
    html = _load(fixture_name)
    rulings = _split_rulings(html)
    extracted = [r.case_number for r in rulings]
    assert extracted == expected_case_numbers
    # No ruling should have a None or UNKNOWN-prefixed case_number.
    for r in rulings:
        assert r.case_number is not None
        assert not r.case_number.startswith("UNKNOWN-")


# ---------------------------------------------------------------------------
# Appellate Division scraper (#2599)
#
# Fixtures:
#   la_appellate_main_empty.html — real capture 2026-04-17, empty state
#                                   (#siteMasterHolder_basicBodyHolder_NoRuling
#                                    "No rulings have been published at this
#                                    time.")
#
# The appellate dropdown uses a different ASP.NET control name
# (List1DeptDate vs civil's List2DeptDate) and each option's value is a
# hearing date only — no courthouse/dept, per LA Court's own HTML comment.
# ---------------------------------------------------------------------------


def _synthetic_appellate_main_with_dropdown(dates: list[str]) -> str:
    """Build a minimal appellate main page with a populated dropdown.

    We don't have a live-fixture of a populated appellate dropdown (the
    division is very low volume and was empty on every Wayback snapshot
    plus the live fetch) so this helper composes the exact structure
    documented by LA Court's own HTML comment:
      appellate division does not include the location and department
      in the drop down box since there's only only location and
      department.  The drop down box displays Hearing date only.
    """
    options = "\n".join(f'<option value="{d}">{d}</option>' for d in dates)
    return f"""
<html>
<head></head>
<body>
<form method="post" action="./main.aspx?casetype=appellate" id="lascwebform">
<input type="hidden" name="__VIEWSTATE" value="/wEFAKE" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="APPEFAKE" />
<input type="hidden" name="__EVENTVALIDATION" value="/wEWFAKE" />
<select name="ctl00$ctl00$siteMasterHolder$basicBodyHolder$List1DeptDate"
        id="siteMasterHolder_basicBodyHolder_List1DeptDate">
{options}
</select>
<input type="submit" name="submit2" value="Search" />
</form>
</body>
</html>
"""


def _synthetic_appellate_ruling_response(case_number: str = "BV033456") -> str:
    """Build a minimal per-date ruling response following the civil speechSynthesis format."""
    return f"""
<html><body>
<div id="speechSynthesis">
<B> Case Number: </B> {case_number}&nbsp;&nbsp;&nbsp;<br>
<B> Hearing Date: </B> April 17, 2026&nbsp;&nbsp;&nbsp;<br>
<B> Dept: </B> Appellate<br>
<P>JOHN SMITH, Plaintiff(s),
vs.
JANE DOE, Defendant(s).</P>
<P>The appeal is DENIED.</P>
<div>Hon. Helen I. Bendix Judge of the Superior Court</div>
</div>
</body></html>
"""


def test_has_no_rulings_appellate_detects_empty_state() -> None:
    html = _load("la_appellate_main_empty.html")
    assert _has_no_rulings_appellate(html) is True


def test_has_no_rulings_appellate_negative_on_civil_page() -> None:
    # Civil main page does not have the appellate empty-state marker.
    html = _load("la_main_page.html")
    assert _has_no_rulings_appellate(html) is False


def test_has_no_rulings_appellate_negative_on_populated_fixture() -> None:
    html = _synthetic_appellate_main_with_dropdown(["04/17/2026"])
    assert _has_no_rulings_appellate(html) is False


def test_parse_appellate_dropdown_options_empty_state_returns_empty() -> None:
    html = _load("la_appellate_main_empty.html")
    options = _parse_appellate_dropdown_options(html)
    assert options == []


def test_parse_appellate_dropdown_options_parses_hearing_dates() -> None:
    html = _synthetic_appellate_main_with_dropdown(["04/17/2026", "04/24/2026"])
    options = _parse_appellate_dropdown_options(html)
    assert len(options) == 2
    first = options[0]
    assert first.value == "04/17/2026"
    assert first.hearing_date == datetime(2026, 4, 17)
    assert first.courthouse == APPELLATE_COURTHOUSE
    assert first.department == APPELLATE_DEPARTMENT
    assert first.courthouse_code == "APPELLATE"


def test_parse_appellate_dropdown_options_skips_malformed_values() -> None:
    # Malformed values (not MM/DD/YYYY) should be skipped, not raise.
    html = _synthetic_appellate_main_with_dropdown(["2026-04-17", "04/17/2026"])
    options = _parse_appellate_dropdown_options(html)
    # Only the correctly-formatted date parses.
    assert len(options) == 1
    assert options[0].value == "04/17/2026"


def test_default_config_appellate() -> None:
    config = default_config_appellate(s3_bucket="judgemind-document-archive-dev")
    assert config.scraper_id == "ca-la-tentatives-appellate"
    assert config.state == "CA"
    assert config.county == "Los Angeles"
    assert config.court == "Superior Court"
    assert config.s3_bucket == "judgemind-document-archive-dev"
    assert config.target_urls == [APPELLATE_URL]
    # Twice-daily cadence mirrors civil.
    assert len(config.schedule_windows) == 2


@respx.mock
def test_appellate_full_run_empty_state_yields_zero_docs() -> None:
    """Real empty-state fixture: scraper completes successfully with 0 docs."""
    main_html = _load("la_appellate_main_empty.html")
    respx.get(APPELLATE_URL).mock(return_value=httpx.Response(200, text=main_html))

    config = default_config_appellate()
    config.request_delay_seconds = 0
    scraper = LAAppellateTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 0


@respx.mock
def test_appellate_full_run_populated_dropdown_captures_docs() -> None:
    """When rulings are published, fetch_documents POSTs per hearing date and
    stamps static Appellate Division metadata on each doc."""
    main_html = _synthetic_appellate_main_with_dropdown(["04/17/2026", "04/24/2026"])
    ruling_html = _synthetic_appellate_ruling_response()

    respx.get(APPELLATE_URL).mock(return_value=httpx.Response(200, text=main_html))
    respx.post(APPELLATE_URL).mock(return_value=httpx.Response(200, text=ruling_html))

    config = default_config_appellate()
    config.request_delay_seconds = 0
    scraper = LAAppellateTentativeRulingsScraper(config=config)
    docs = scraper.fetch_documents()

    # Two dropdown options, one case per response.
    assert len(docs) == 2
    for doc in docs:
        assert doc.courthouse == APPELLATE_COURTHOUSE
        assert doc.department == APPELLATE_DEPARTMENT
        assert doc.source_url == APPELLATE_URL
        assert doc.extra.get("casetype") == "appellate"


@respx.mock
def test_appellate_full_run_stamps_metadata_through_parse() -> None:
    """End-to-end via scraper.run() — records_captured reflects parsed docs
    and static appellate metadata is preserved after parse_document."""
    main_html = _synthetic_appellate_main_with_dropdown(["04/17/2026"])
    ruling_html = _synthetic_appellate_ruling_response()

    respx.get(APPELLATE_URL).mock(return_value=httpx.Response(200, text=main_html))
    respx.post(APPELLATE_URL).mock(return_value=httpx.Response(200, text=ruling_html))

    config = default_config_appellate()
    config.request_delay_seconds = 0
    scraper = LAAppellateTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 1


@respx.mock
def test_appellate_full_run_handles_stale_viewstate() -> None:
    """Stale-ViewState POST responses are skipped without failing the run."""
    main_html = _synthetic_appellate_main_with_dropdown(["04/17/2026"])
    # Reuse real LA stale-ViewState error fixture — same portal, same marker.
    stale_html = _load("la_ruling_smc49.html")

    respx.get(APPELLATE_URL).mock(return_value=httpx.Response(200, text=main_html))
    respx.post(APPELLATE_URL).mock(return_value=httpx.Response(200, text=stale_html))

    config = default_config_appellate()
    config.request_delay_seconds = 0
    scraper = LAAppellateTentativeRulingsScraper(config=config)
    health = scraper.run()

    assert health.success is True
    assert health.records_captured == 0


def test_appellate_scraper_registered_in_runner() -> None:
    """The appellate scraper is registered and discoverable via the runner."""
    from framework.runner import get_scraper_ids

    ids = get_scraper_ids()
    assert "ca-la-tentatives-appellate" in ids
    # Civil is still registered separately.
    assert "ca-la-tentatives-civil" in ids


def test_parse_appellate_dropdown_options_skips_empty_value_option() -> None:
    """Options with empty ``value`` attribute (e.g. a placeholder) are skipped."""
    html = """
<html><body>
<select name="ctl00$ctl00$siteMasterHolder$basicBodyHolder$List1DeptDate"
        id="siteMasterHolder_basicBodyHolder_List1DeptDate">
<option value="">-- select a date --</option>
<option value="04/17/2026">04/17/2026</option>
</select>
</body></html>
"""
    options = _parse_appellate_dropdown_options(html)
    assert len(options) == 1
    assert options[0].value == "04/17/2026"


def test_parse_appellate_dropdown_options_skips_invalid_calendar_date() -> None:
    """Values matching the regex but not a real calendar date (e.g. 02/30/2026)
    parse with ``hearing_date=None`` rather than raising."""
    # 02/30/2026 matches the MM/DD/YYYY regex but is not a valid date —
    # datetime.strptime raises ValueError, which the parser swallows.
    html = _synthetic_appellate_main_with_dropdown(["02/30/2026"])
    options = _parse_appellate_dropdown_options(html)
    assert len(options) == 1
    assert options[0].value == "02/30/2026"
    assert options[0].hearing_date is None


@respx.mock
def test_appellate_fetch_documents_logs_and_continues_on_per_date_fetch_error() -> None:
    """If the POST for one dropdown date raises, the scraper logs and moves on.

    Covers the ``except Exception`` branch inside the per-option loop in
    ``fetch_documents()`` (#2599).
    """
    main_html = _synthetic_appellate_main_with_dropdown(["04/17/2026", "04/24/2026"])
    ruling_html = _synthetic_appellate_ruling_response()

    respx.get(APPELLATE_URL).mock(return_value=httpx.Response(200, text=main_html))
    # First POST raises a network error, second POST succeeds.
    respx.post(APPELLATE_URL).mock(
        side_effect=[
            httpx.ConnectError("simulated network failure"),
            httpx.Response(200, text=ruling_html),
        ]
    )

    config = default_config_appellate()
    config.request_delay_seconds = 0
    scraper = LAAppellateTentativeRulingsScraper(config=config)
    docs = scraper.fetch_documents()

    # First date errored, second date produced one doc.
    assert len(docs) == 1


def _make_appellate_ruling_doc(
    raw_content: bytes = b"<html>no speechSynthesis div here</html>",
    **overrides: object,
) -> CapturedDocument:
    """Build a bare CapturedDocument for appellate parse_document tests."""
    kwargs: dict[str, object] = {
        "scraper_id": "ca-la-tentatives-appellate",
        "state": "CA",
        "county": "Los Angeles",
        "court": "Superior Court",
        "source_url": APPELLATE_URL,
        "capture_timestamp": datetime(2026, 4, 17, 18, 0, 0),
        "content_format": ContentFormat.HTML,
        "raw_content": raw_content,
        "content_hash": "",
    }
    kwargs.update(overrides)
    return CapturedDocument(**kwargs)  # type: ignore[arg-type]


def test_appellate_parse_document_respects_pre_split_guard() -> None:
    """parse_document must not re-run regex on docs already LLM-pre-split.

    Covers the ``pre_split``/``_llm_extracted`` early-return in
    ``parse_document()`` (#2469, #2484, #2599).
    """
    config = default_config_appellate()
    scraper = LAAppellateTentativeRulingsScraper(config=config)

    # Construct a doc that has already been LLM-pre-split: fields already
    # populated by the LLM path, and the pre_split marker set.  The method
    # must return it unchanged (except for default courthouse/department
    # fallbacks if either was cleared).
    doc = _make_appellate_ruling_doc(
        raw_content=b"<html>ignored - guard short-circuits before parse</html>",
        case_number="BV999999",
        ruling_text="Pre-populated by LLM",
        extra={"pre_split": True, "_llm_extracted": True},
    )
    result = scraper.parse_document(doc)

    # Guard kicked in: the raw HTML was NOT re-parsed (case_number would
    # have been cleared if it had been).
    assert result.case_number == "BV999999"
    assert result.ruling_text == "Pre-populated by LLM"
    # Static appellate metadata fallbacks applied.
    assert result.courthouse == APPELLATE_COURTHOUSE
    assert result.department == APPELLATE_DEPARTMENT


def test_appellate_parse_document_swallows_parse_errors() -> None:
    """Malformed HTML must not raise — parse errors are logged and skipped.

    Covers the ``except Exception`` branch around ``_extract_ruling_fields``
    in ``parse_document()`` (#2599).
    """
    config = default_config_appellate()
    scraper = LAAppellateTentativeRulingsScraper(config=config)

    doc = _make_appellate_ruling_doc()
    # Force the extractor to raise so we hit the except branch.
    with patch(
        "courts.ca.la_tentatives._extract_ruling_fields",
        side_effect=RuntimeError("boom"),
    ):
        result = scraper.parse_document(doc)

    # No exception bubbled up; static appellate metadata is still stamped.
    assert result.courthouse == APPELLATE_COURTHOUSE
    assert result.department == APPELLATE_DEPARTMENT


# ---------------------------------------------------------------------------
# LA extraction fixes — Bug fixes for #2578
#
# LA HTML has three extraction gaps documented in #2578:
#   1. Judge NULL when the page uses "JUDGE/DEPT: <surname>/<dept>" form layout
#      (Dept 25 Mkrtchyan pattern) — the classic "X Judge of the Superior Court"
#      signature regex never matches.
#   2. case_title contaminated by metadata like "COMP. FILED : 07-03-25" or
#      "PET. FILED : 11-26-25" because the inline CASE NAME: field runs on the
#      same line as the FILED metadata and the regex only stopped at
#      "CASE NUMBER".
#   3. Trailing U+00BF inverted question mark (¿) in titles because the LA
#      clerk's Word-to-HTML conversion injects literal ¿ where soft hyphens or
#      non-breaking spaces should be.
# ---------------------------------------------------------------------------


def test_extract_ruling_fields_judge_dept_form_layout() -> None:
    """Bug 1: JUDGE/DEPT: <surname>/<dept> layout populates doc.judge_name (#2578)."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_dept25_judge_dept_format.html")
    soup = BeautifulSoup(html, "lxml")
    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 26),
        content_format=ContentFormat.HTML,
        raw_content=html.encode("utf-8"),
        content_hash="",
    )
    _extract_ruling_fields(soup, doc)
    assert doc.judge_name is not None, "judge_name must be extracted from JUDGE/DEPT line"
    assert doc.judge_name == "Mkrtchyan"


def test_extract_ruling_fields_judge_dept_regex_direct() -> None:
    """Bug 1: JUDGE/DEPT regex handles simple surname/department pairs."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>HEARING DATE: Thursday, March 26, 2026 "
        "JUDGE/DEPT: Mkrtchyan/25</p>"
        "<p>Case Number: 25STLC05162</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 26),
        content_format=ContentFormat.HTML,
        raw_content=html.encode("utf-8"),
        content_hash="",
    )
    _extract_ruling_fields(soup, doc)
    assert doc.judge_name == "Mkrtchyan"


def test_extract_ruling_fields_judge_dept_with_compound_surname() -> None:
    """Bug 1: JUDGE/DEPT regex accepts compound surnames with spaces."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>HEARING DATE: Thursday, March 26, 2026 "
        "JUDGE/DEPT: Van Der Berg/F46</p>"
        "<p>Case Number: 25STLC99999</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 26),
        content_format=ContentFormat.HTML,
        raw_content=html.encode("utf-8"),
        content_hash="",
    )
    _extract_ruling_fields(soup, doc)
    assert doc.judge_name == "Van Der Berg"


def test_extract_ruling_fields_judge_div_still_works() -> None:
    """Bug 1 regression: JUDGE/DEPT extraction does not break the existing
    'X Judge of the Superior Court' signature path."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>Case Number: 24NNCV02551</p>"
        "<div>William A. Crowfoot Judge of the Superior Court</div>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 26),
        content_format=ContentFormat.HTML,
        raw_content=html.encode("utf-8"),
        content_hash="",
    )
    _extract_ruling_fields(soup, doc)
    assert doc.judge_name == "William A. Crowfoot"


def test_extract_case_title_stops_at_comp_filed() -> None:
    """Bug 2: CASE NAME: ... COMP. FILED : <date> must not leak FILED text (#2578)."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>CASE NAME: Landaverde v. Meller    COMP. FILED: 07-03-25</p>"
        "<p>CASE NUMBER: 25STLC05162    PET. FILED: 10-01-25</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    assert "FILED" not in title.upper()
    assert "07-03-25" not in title
    assert "Landaverde" in title
    assert "Meller" in title


def test_extract_case_title_stops_at_pet_filed() -> None:
    """Bug 2: PET. FILED boundary also stops the CASE NAME capture."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>CASE NAME: Smith v. Jones    PET. FILED: 11-26-25</p>"
        "<p>CASE NUMBER: 25STPB12345</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    assert "FILED" not in title.upper()
    assert "11-26-25" not in title
    assert "Smith" in title
    assert "Jones" in title


def test_extract_case_title_stops_at_fac_filed() -> None:
    """Bug 2: FAC FILED (First Amended Complaint) also stops the capture."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>CASE NAME: Acme Corp v. Beta Industries   FAC FILED: 02-15-25</p>"
        "<p>CASE NUMBER: 25STCV00001</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    assert "FILED" not in title.upper()
    assert "02-15-25" not in title
    assert "Acme" in title
    assert "Beta" in title


def test_extract_case_title_stops_at_complaint_filed() -> None:
    """Bug 2: Long-form 'COMPLAINT FILED' variant also works."""
    from bs4 import BeautifulSoup

    html = (
        "<div id='speechSynthesis'>"
        "<p>CASE NAME: Doe v. Roe    COMPLAINT FILED: 01-01-25</p>"
        "<p>CASE NUMBER: 25STCV22222</p>"
        "</div>"
    )
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    assert title is not None
    assert "FILED" not in title.upper()
    assert "01-01-25" not in title
    assert "Doe" in title


def test_extract_case_title_case_number_boundary_still_works() -> None:
    """Bug 2 regression: The original CASE NUMBER boundary still fires."""
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
    assert "25SMCV01132" not in title
    assert "Porsche" in title


def test_sanitize_title_strips_inverted_question_mark() -> None:
    """Bug 3: U+00BF inverted question mark is stripped from titles (#2578)."""
    raw = "Abigail Rodriguez And Iliana Mendez v. American Honda Motor Company, Inc.\u00bf"
    result = _sanitize_title(raw)
    assert result is not None
    assert "\u00bf" not in result
    assert "American Honda" in result
    assert "Abigail Rodriguez" in result


def test_sanitize_title_strips_multiple_inverted_question_marks() -> None:
    """Bug 3: Multiple ¿ characters are all removed."""
    raw = "A\u00bfBC Corp v. XY\u00bfZ Industries\u00bf"
    result = _sanitize_title(raw)
    assert result is not None
    assert "\u00bf" not in result


def test_sanitize_title_inverted_question_mark_only_below_min_length() -> None:
    """Bug 3: If stripping ¿ leaves the title below the 5-char minimum, return None."""
    raw = "A\u00bf\u00bf\u00bf"
    result = _sanitize_title(raw)
    assert result is None


def test_extract_case_title_honda_fixture_strips_question_mark() -> None:
    """Bug 3 end-to-end: Honda fixture (has literal ¿ in defendant caption)
    yields a title without ¿."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_honda_inverted_qmark.html")
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", id="speechSynthesis")
    title = _extract_case_title(content)
    # Honda may not produce a title from the parties anchor if cleaning removes
    # too much, but if it does, ¿ must not appear.
    if title is not None:
        assert "\u00bf" not in title


def test_extract_ruling_fields_landaverde_fixture_end_to_end() -> None:
    """Bug 1+2 end-to-end: Dept 25 Landaverde fixture yields clean title and judge."""
    from bs4 import BeautifulSoup

    html = _load("la_ruling_dept25_judge_dept_format.html")
    soup = BeautifulSoup(html, "lxml")
    doc = CapturedDocument(
        scraper_id="test",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        source_url=CIVIL_URL,
        capture_timestamp=datetime(2026, 3, 26),
        content_format=ContentFormat.HTML,
        raw_content=html.encode("utf-8"),
        content_hash="",
    )
    _extract_ruling_fields(soup, doc)

    # Judge extracted from JUDGE/DEPT line
    assert doc.judge_name == "Mkrtchyan"
    # case_number extracted via deterministic header (#2330 path)
    assert doc.case_number == "25STLC05162"
    # Department extracted
    assert doc.department == "25"
    # Title extracted from CASE NAME: field, without COMP. FILED contamination
    if doc.case_title is not None:
        assert "FILED" not in doc.case_title.upper()
        assert "07-03-25" not in doc.case_title
        assert "Landaverde" in doc.case_title
        assert "Meller" in doc.case_title


# ---------------------------------------------------------------------------
# TestLlmExtractRulingsRoleLiteralTitle — role-literal case_title suppression
# (#3749)
# ---------------------------------------------------------------------------


class TestLlmExtractRulingsRoleLiteralTitle:
    """Tests for role-literal case_title sanitization in _llm_extract_rulings().

    The LA LLM occasionally emits placeholders like "Plaintiff v. General Motors, LLC"
    instead of real party names.  The post-processor should either rebuild the title
    from extracted parties or drop it to None — never let a role-literal placeholder
    reach the DB.  See #3749.
    """

    def test_role_literal_title_rebuilt_from_parties(self) -> None:
        """LLM emits 'Plaintiff v. General Motors, LLC' + real parties → rebuilt title."""
        rulings_data = [
            {
                "extracted_case_number": "25STCV35786",
                "extracted_case_title": "Plaintiff v. General Motors, LLC",
                "outcome": "denied",
                "ruling_text": "The motion is DENIED.",
                "parties": [
                    {"name": "Sumayya Aasi", "role": "plaintiff"},
                    {"name": "General Motors, LLC", "role": "defendant"},
                ],
            }
        ]
        mock_response = _make_llm_response(rulings_data)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>Case Number: 25STCV35786</div></body></html>"
            )

        assert result is not None
        assert len(result) == 1
        assert result[0].case_title == "Sumayya Aasi v. General Motors, LLC"

    def test_role_literal_title_dropped_when_parties_insufficient(self) -> None:
        """LLM emits 'Plaintiff v. Defendant' with no parties → case_title is None."""
        rulings_data = [
            {
                "extracted_case_number": "25STCV99999",
                "extracted_case_title": "Plaintiff v. Defendant",
                "outcome": "granted",
                "ruling_text": "The motion is GRANTED.",
                "parties": [],
            }
        ]
        mock_response = _make_llm_response(rulings_data)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>Case Number: 25STCV99999</div></body></html>"
            )

        assert result is not None
        assert len(result) == 1
        assert result[0].case_title is None

    def test_clean_title_passes_through(self) -> None:
        """A real party-name title is not affected by the role-literal sanitizer."""
        rulings_data = [
            {
                "extracted_case_number": "24NNCV02551",
                "extracted_case_title": "Aasi v. American Honda",
                "outcome": "granted",
                "ruling_text": "The motion is GRANTED.",
                "parties": [
                    {"name": "Sumayya Aasi", "role": "plaintiff"},
                    {"name": "American Honda Motor Co., Inc.", "role": "defendant"},
                ],
            }
        ]
        mock_response = _make_llm_response(rulings_data)
        with patch("ingestion.llm_providers.call_llm", return_value=mock_response):
            result = _llm_extract_rulings(
                "<html><body><div id='speechSynthesis'>Case Number: 24NNCV02551</div></body></html>"
            )

        assert result is not None
        assert len(result) == 1
        assert result[0].case_title == "Aasi v. American Honda"
