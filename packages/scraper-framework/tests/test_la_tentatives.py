"""Tests for LA County tentative ruling scraper — built against real fixture HTML.

Fixtures captured from live site 2026-03-02:
  la_main_page.html   — GET https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil
  la_ruling_response.html — POST for ALH,3,03/02/2026 (Alhambra Dept 3)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

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
    _is_name_fragment,
    _is_stale_viewstate_response,
    _parse_dropdown_options,
    _parse_option,
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


def test_split_cases_html_no_speech_div_returns_original() -> None:
    """HTML without div#speechSynthesis should return the original HTML."""
    html = "<html><body><p>No rulings here.</p></body></html>"
    sections = _split_cases_html(html)
    assert len(sections) == 1
    assert sections[0] == html


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
