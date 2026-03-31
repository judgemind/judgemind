"""Tests for San Francisco civil tentative rulings scraper.

Fixtures captured from webapps.sftc.org/tr/tr.dll via manual browsing
(site is behind Cloudflare Turnstile CAPTCHA):

  sf-civil-api-response-rid10-2026-03-23.json — 12 rulings, Dept 301
  sf-civil-api-response-rid10-2026-03-24.json — 11 rulings, Dept 301
  sf-civil-api-response-rid10-2026-03-25.json — 10 rulings, Dept 301

All fixtures are for RulingID=10 (Dept 301, Law & Motion/Discovery).
Other RulingIDs use the same response format.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.sf_civil_tentatives import (
    CIVIL_API_URL,
    RULING_ID_MAP,
    RULING_IDS,
    SFCivilTentativeRulingsScraper,
    _clean_party_name,
    extract_outcome,
    extract_parties_from_title,
    normalize_case_title,
    parse_api_response,
    parse_hearing_date,
)
from courts.ca.sf_civil_tentatives import default_config as sf_civil_default_config

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures" / "sf-human-captures"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_api_response — against real fixture data
# ---------------------------------------------------------------------------


class TestParseApiResponse:
    """Tests for parse_api_response against real fixture data."""

    def test_parses_correct_count(self) -> None:
        """Fixture has 12 rulings — parser should find all 12."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert len(rulings) == 12

    def test_parses_second_fixture(self) -> None:
        """Second fixture has 11 rulings."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-24.json")
        rulings = parse_api_response(json_text)
        assert len(rulings) == 11

    def test_parses_third_fixture(self) -> None:
        """Third fixture has 10 rulings."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-25.json")
        rulings = parse_api_response(json_text)
        assert len(rulings) == 10

    def test_extracts_case_number(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].case_number == "CPF23518377"

    def test_extracts_case_title(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].case_title is not None
        assert "MAIBACH" in rulings[0].case_title
        assert "VS." in rulings[0].case_title

    def test_extracts_court_date(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].court_date == "2026-03-23 09:00 AM"

    def test_extracts_calendar_matter(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].calendar_matter is not None
        assert len(rulings[0].calendar_matter) > 10

    def test_extracts_ruling_text(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].ruling_text is not None
        assert len(rulings[0].ruling_text) > 50

    def test_extracts_ruling_html(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].ruling_html is not None
        assert len(rulings[0].ruling_html) > 0

    def test_extracts_case_info_url(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].case_info_url is not None
        assert "CaseInfo.dll" in rulings[0].case_info_url
        assert "CPF23518377" in rulings[0].case_info_url

    def test_all_rulings_have_case_numbers(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        for ruling in rulings:
            assert ruling.case_number is not None, "Every ruling should have a case number"
            assert len(ruling.case_number) > 0

    def test_all_rulings_have_case_titles(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        for ruling in rulings:
            assert ruling.case_title is not None, "Every ruling should have a case title"

    def test_all_rulings_have_ruling_text(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        for ruling in rulings:
            assert ruling.ruling_text is not None, "Every ruling should have ruling text"
            assert len(ruling.ruling_text) > 20

    def test_skips_leading_non_ruling_rows(self) -> None:
        """Leading rows before the first 'Case Number:' should be ignored."""
        html = (
            '<tr><td class="dataHeader">Notice:</td><td>Header row</td></tr>'
            '<tr><td class="dataHeader">Case Number:</td><td></td>'
            '<td><a href="test">ABC123</a></td></tr>'
            '<tr><td class="dataHeader">Case Title:</td><td></td>'
            "<td>Test v. Case</td></tr>"
        )
        json_text = '{"result": [1, "' + html.replace('"', '\\"') + '"]}'
        rulings = parse_api_response(json_text)
        assert len(rulings) == 1
        assert rulings[0].case_number == "ABC123"

    def test_handles_empty_result(self) -> None:
        rulings = parse_api_response('{"result": [0, ""]}')
        assert rulings == []

    def test_handles_invalid_json(self) -> None:
        rulings = parse_api_response("not json at all")
        assert rulings == []

    def test_handles_missing_result_key(self) -> None:
        rulings = parse_api_response('{"other": "data"}')
        assert rulings == []

    def test_second_ruling_case_number(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[1].case_number == "CGC22602981"


# ---------------------------------------------------------------------------
# parse_hearing_date
# ---------------------------------------------------------------------------


class TestParseHearingDate:
    """Tests for hearing date parsing."""

    def test_standard_format(self) -> None:
        dt = parse_hearing_date("2026-03-23 09:00 AM")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 23

    def test_date_only(self) -> None:
        dt = parse_hearing_date("2026-03-23")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 23

    def test_mm_dd_yyyy(self) -> None:
        dt = parse_hearing_date("03/23/2026")
        assert dt is not None
        assert dt.year == 2026

    def test_none_input(self) -> None:
        assert parse_hearing_date(None) is None

    def test_empty_string(self) -> None:
        assert parse_hearing_date("") is None

    def test_invalid_format(self) -> None:
        assert parse_hearing_date("not a date") is None


# ---------------------------------------------------------------------------
# extract_outcome
# ---------------------------------------------------------------------------


class TestExtractOutcome:
    """Tests for outcome extraction from ruling text."""

    def test_granted(self) -> None:
        assert extract_outcome("The motion is granted.") == "granted"

    def test_denied(self) -> None:
        assert extract_outcome("The motion is denied.") == "denied"

    def test_sustained(self) -> None:
        assert extract_outcome("The demurrer is sustained.") == "granted"

    def test_overruled(self) -> None:
        assert extract_outcome("The demurrer is overruled.") == "denied"

    def test_continued(self) -> None:
        assert extract_outcome("The hearing is continued to April 6.") == "continued"

    def test_moot(self) -> None:
        assert extract_outcome("The motion is moot.") == "moot"

    def test_off_calendar(self) -> None:
        assert extract_outcome("This matter is taken off calendar.") == "off_calendar"

    def test_none_for_no_match(self) -> None:
        assert extract_outcome("The court reserves judgment.") is None

    def test_none_for_empty(self) -> None:
        assert extract_outcome(None) is None

    def test_from_real_fixture(self) -> None:
        """Extract outcome from real fixture ruling that contains 'continued'."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        # First ruling mentions "is continued"
        outcome = extract_outcome(rulings[0].ruling_text)
        assert outcome == "continued"


# ---------------------------------------------------------------------------
# extract_parties_from_title
# ---------------------------------------------------------------------------


class TestExtractPartiesFromTitle:
    """Tests for party extraction from case titles."""

    def test_simple_vs(self) -> None:
        parties = extract_parties_from_title("HOWARD I. MAIBACH VS. THE REGENTS")
        assert len(parties) == 2
        assert parties[0]["role"] == "plaintiff"
        assert parties[1]["role"] == "defendant"
        assert "Maibach" in parties[0]["name"]

    def test_with_et_al(self) -> None:
        parties = extract_parties_from_title(
            "CENTER FOR ADVANCED PUBLIC AWARENESS VS. PARK LIFE DESIGNS, LLC ET AL"
        )
        assert len(parties) == 2
        assert "Et Al" not in parties[1]["name"]

    def test_no_vs(self) -> None:
        parties = extract_parties_from_title("IN RE ESTATE OF JOHN DOE")
        assert parties == []

    def test_none_input(self) -> None:
        parties = extract_parties_from_title(None)
        assert parties == []

    def test_empty_string(self) -> None:
        parties = extract_parties_from_title("")
        assert parties == []

    def test_title_cased(self) -> None:
        parties = extract_parties_from_title("JOHN SMITH VS. JANE DOE")
        assert parties[0]["name"] == "John Smith"
        assert parties[1]["name"] == "Jane Doe"


# ---------------------------------------------------------------------------
# _clean_party_name
# ---------------------------------------------------------------------------


class TestCleanPartyName:
    """Tests for party name cleaning."""

    def test_strips_et_al(self) -> None:
        assert _clean_party_name("PARK LIFE DESIGNS, LLC ET AL") == "Park Life Designs, Llc"

    def test_strips_et_al_with_period(self) -> None:
        assert _clean_party_name("SOME CORP, ET AL.") == "Some Corp"

    def test_title_cases_all_caps(self) -> None:
        assert _clean_party_name("JOHN SMITH") == "John Smith"

    def test_preserves_mixed_case(self) -> None:
        assert _clean_party_name("John Smith") == "John Smith"

    def test_strips_trailing_comma(self) -> None:
        result = _clean_party_name("JOHN SMITH,")
        assert result is not None
        assert not result.endswith(",")

    def test_returns_none_for_empty(self) -> None:
        assert _clean_party_name("") is None


# ---------------------------------------------------------------------------
# normalize_case_title
# ---------------------------------------------------------------------------


class TestNormalizeCaseTitle:
    """Tests for case title normalization."""

    def test_title_cases_all_caps(self) -> None:
        result = normalize_case_title("HOWARD I. MAIBACH VS. THE REGENTS")
        assert result == "Howard I. Maibach Vs. The Regents"

    def test_preserves_mixed_case(self) -> None:
        result = normalize_case_title("Howard I. Maibach vs. The Regents")
        assert result == "Howard I. Maibach vs. The Regents"

    def test_none_input(self) -> None:
        assert normalize_case_title(None) is None

    def test_empty_string(self) -> None:
        assert normalize_case_title("") is None

    def test_normalizes_whitespace(self) -> None:
        result = normalize_case_title("JOHN   SMITH  VS.  JANE   DOE")
        assert result is not None
        assert "  " not in result


# ---------------------------------------------------------------------------
# RULING_ID_MAP
# ---------------------------------------------------------------------------


class TestRulingIdMap:
    """Tests for the RulingID-to-department mapping."""

    def test_seven_ruling_ids(self) -> None:
        assert len(RULING_ID_MAP) == 7

    def test_all_ids_present(self) -> None:
        expected_ids = {10, 2, 6, 5, 8, 9, 3}
        assert set(RULING_ID_MAP.keys()) == expected_ids

    def test_dept_301(self) -> None:
        assert RULING_ID_MAP[10]["department"] == "301"

    def test_dept_302(self) -> None:
        assert RULING_ID_MAP[2]["department"] == "302"

    def test_dept_304_multiple_ids(self) -> None:
        """Dept 304 has 3 RulingIDs (5, 6, 8) for different calendar types."""
        dept_304_ids = [rid for rid, info in RULING_ID_MAP.items() if info["department"] == "304"]
        assert sorted(dept_304_ids) == [5, 6, 8]

    def test_dept_210(self) -> None:
        assert RULING_ID_MAP[9]["department"] == "210"

    def test_dept_501(self) -> None:
        assert RULING_ID_MAP[3]["department"] == "501"

    def test_ruling_ids_sorted(self) -> None:
        """RULING_IDS list should be sorted."""
        assert RULING_IDS == sorted(RULING_IDS)


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    """Tests for the default_config factory."""

    def test_scraper_id(self) -> None:
        config = sf_civil_default_config()
        assert config.scraper_id == "ca-sf-tentatives-civil"

    def test_state_county(self) -> None:
        config = sf_civil_default_config()
        assert config.state == "CA"
        assert config.county == "San Francisco"

    def test_s3_bucket(self) -> None:
        config = sf_civil_default_config(s3_bucket="test-bucket")
        assert config.s3_bucket == "test-bucket"

    def test_schedule_windows(self) -> None:
        config = sf_civil_default_config()
        assert len(config.schedule_windows) == 2


# ---------------------------------------------------------------------------
# Full scraper — mocked HTTP using real fixtures
# ---------------------------------------------------------------------------


class TestSFCivilScraperRun:
    """Integration tests for the full scraper run with mocked HTTP."""

    @respx.mock
    def test_full_run_success(self) -> None:
        """Full run with one RulingID returning data — should succeed."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        # Mock all POST requests to return the same fixture
        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text=json_text))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        health = scraper.run()
        assert health.success is True
        # 7 RulingIDs * 12 rulings each = 84
        assert health.records_captured == 7 * 12

    @respx.mock
    def test_fetch_documents_populates_fields(self) -> None:
        """Verify all required fields are populated on fetched documents."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text=json_text))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        docs = scraper.fetch_documents()
        assert len(docs) > 0

        # Check first document from RulingID=2 (sorted first)
        first = docs[0]
        assert first.case_number is not None
        assert first.case_title is not None
        assert first.department is not None
        assert first.hearing_date is not None
        assert first.ruling_text is not None
        assert first.courthouse == "San Francisco Courthouse"

    @respx.mock
    def test_fetch_documents_sets_department(self) -> None:
        """Each document should have the correct department from RULING_ID_MAP."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text=json_text))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        docs = scraper.fetch_documents()
        departments = {d.department for d in docs}
        # All 5 departments should be represented (each RulingID returns data)
        assert departments == {"210", "301", "302", "304", "501"}

    @respx.mock
    def test_fetch_documents_sets_judge_name_from_dept_map(self) -> None:
        """Judge name should be populated from dept_judge_map when provided."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text=json_text))

        dept_judge_map = {
            "301": "Curtis E.A. Karnow",
            "302": "Ethan P. Schulman",
            "304": "Anne-Christine Massullo",
            "210": "Samuel K. Feng",
            "501": "Richard B. Ulmer",
        }
        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config, dept_judge_map=dept_judge_map)

        docs = scraper.fetch_documents()
        # All documents should have judge names
        has_judge = [d for d in docs if d.judge_name]
        assert len(has_judge) == len(docs)
        # Check specific department → judge mapping
        dept_301_docs = [d for d in docs if d.department == "301"]
        assert dept_301_docs[0].judge_name == "Curtis E.A. Karnow"

    @respx.mock
    def test_fetch_documents_without_dept_map(self) -> None:
        """Judge name should be None when no dept_judge_map is provided."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text=json_text))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        docs = scraper.fetch_documents()
        # Without dept_judge_map, judge_name should be None
        for doc in docs:
            assert doc.judge_name is None

    @respx.mock
    def test_fetch_documents_sets_parties(self) -> None:
        """Documents with 'VS.' in title should have parties extracted."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text=json_text))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        docs = scraper.fetch_documents()
        # Most SF civil rulings have VS. in the title
        has_parties = [d for d in docs if d.parties]
        assert len(has_parties) > 0
        first_with_parties = has_parties[0]
        roles = {p["role"] for p in first_with_parties.parties}
        assert "plaintiff" in roles
        assert "defendant" in roles

    @respx.mock
    def test_fetch_documents_sets_motion_type(self) -> None:
        """Calendar matter should be stored as motion_type."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text=json_text))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        docs = scraper.fetch_documents()
        has_motion = [d for d in docs if d.motion_type]
        assert len(has_motion) > 0

    @respx.mock
    def test_fetch_documents_sets_ruling_text_html(self) -> None:
        """ruling_text_html should contain HTML formatting."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text=json_text))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        docs = scraper.fetch_documents()
        has_html = [d for d in docs if d.ruling_text_html]
        assert len(has_html) > 0

    @respx.mock
    def test_fetch_documents_stores_ruling_id_in_extra(self) -> None:
        """Extra metadata should include ruling_id and dept_type."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text=json_text))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        docs = scraper.fetch_documents()
        first = docs[0]
        assert "ruling_id" in first.extra
        assert "dept_type" in first.extra

    @respx.mock
    def test_handles_captcha_redirect(self) -> None:
        """CAPTCHA redirect (302 to captcha.dll) should be handled gracefully."""
        respx.post(CIVIL_API_URL).mock(
            return_value=httpx.Response(
                302,
                headers={"Location": "https://webapps.sftc.org/tr/captcha.dll"},
            )
        )

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        docs = scraper.fetch_documents()
        assert docs == []

    @respx.mock
    def test_handles_http_error_gracefully(self) -> None:
        """HTTP errors per-RulingID are logged but don't crash the run.

        The scraper catches per-RulingID errors and continues to the next.
        The overall run succeeds with 0 records (no unhandled exception).
        """
        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(500))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        health = scraper.run()
        # Per-RulingID errors are caught — the run itself succeeds
        assert health.success is True
        assert health.records_captured == 0

    @respx.mock
    def test_handles_empty_results(self) -> None:
        """Empty result set should succeed with 0 records."""
        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text='{"result": [0, ""]}'))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        health = scraper.run()
        assert health.success is True
        assert health.records_captured == 0

    @respx.mock
    def test_content_format_is_html(self) -> None:
        """Documents should have HTML content format."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        respx.post(CIVIL_API_URL).mock(return_value=httpx.Response(200, text=json_text))

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(config=config)

        docs = scraper.fetch_documents()
        for doc in docs:
            assert doc.content_format.value == "html"


# ---------------------------------------------------------------------------
# Regression tests — specific values from real fixture data
# ---------------------------------------------------------------------------


class TestFixtureRegression:
    """Regression tests asserting specific field values from fixtures."""

    def test_first_ruling_case_number(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].case_number == "CPF23518377"

    def test_first_ruling_case_title_contains_maibach(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].case_title is not None
        assert "MAIBACH" in rulings[0].case_title

    def test_second_ruling_case_number(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[1].case_number == "CGC22602981"

    def test_second_ruling_has_withdrawal_motion(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[1].calendar_matter is not None
        assert "WITHDRAWAL" in rulings[1].calendar_matter.upper()

    def test_court_date_format(self) -> None:
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].court_date == "2026-03-23 09:00 AM"

    def test_case_numbers_may_repeat_for_multiple_motions(self) -> None:
        """A case number can appear multiple times when a case has multiple calendar matters.

        CGC25624250 appears 3 times in the 03-23 fixture — this is correct behavior,
        each entry is a separate motion/hearing for the same case.
        """
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        case_numbers = [r.case_number for r in rulings]
        # 12 rulings but only 10 unique case numbers (CGC25624250 x3)
        assert len(case_numbers) == 12
        assert len(set(case_numbers)) == 10

    def test_fixture_24_has_different_case_numbers(self) -> None:
        """The 03-24 fixture should have different cases from 03-23."""
        json_23 = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        json_24 = _load_fixture("sf-civil-api-response-rid10-2026-03-24.json")
        rulings_23 = parse_api_response(json_23)
        rulings_24 = parse_api_response(json_24)
        cases_23 = {r.case_number for r in rulings_23}
        cases_24 = {r.case_number for r in rulings_24}
        # Different dates should have mostly different cases
        assert cases_23 != cases_24

    def test_ruling_text_contains_calendar_reference(self) -> None:
        """Ruling text should reference the calendar date."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        rulings = parse_api_response(json_text)
        assert rulings[0].ruling_text is not None
        assert "March" in rulings[0].ruling_text

    def test_all_rulings_across_all_fixtures(self) -> None:
        """Total ruling count across all 3 fixtures."""
        total = 0
        for name in [
            "sf-civil-api-response-rid10-2026-03-23.json",
            "sf-civil-api-response-rid10-2026-03-24.json",
            "sf-civil-api-response-rid10-2026-03-25.json",
        ]:
            rulings = parse_api_response(_load_fixture(name))
            total += len(rulings)
        assert total == 33  # 12 + 11 + 10
