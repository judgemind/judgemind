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
from typing import Any

import httpx
import pytest
import respx

from courts.ca.sf_civil_tentatives import (
    CAPSOLVER_API_KEY_ENV_VAR,
    CIVIL_REST_BASE,
    RULING_ID_MAP,
    RULING_IDS,
    SD_PROXY_URL_ENV_VAR,
    SF_PROXY_URL_ENV_VAR,
    TURNSTILE_SITEKEY,
    TURNSTILE_TIMEOUT,
    SFCivilTentativeRulingsScraper,
    _clean_party_name,
    extract_outcome,
    extract_parties_from_title,
    extract_session_id,
    normalize_case_title,
    parse_api_response,
    parse_hearing_date,
)
from courts.ca.sf_civil_tentatives import default_config as sf_civil_default_config

# Fake session ID for tests — bypasses Playwright session acquisition.
TEST_SESSION_ID = "AABBCCDD1122334455667788AABBCCDD11223344"

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

    def test_eight_ruling_ids(self) -> None:
        assert len(RULING_ID_MAP) == 8

    def test_all_ids_present(self) -> None:
        expected_ids = {10, 2, 6, 5, 8, 9, 3, 7}
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

    def test_dept_204_probate(self) -> None:
        """RulingID 7 maps to Dept 204 (Probate)."""
        assert RULING_ID_MAP[7]["department"] == "204"
        assert RULING_ID_MAP[7]["type"] == "Probate"

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
# extract_session_id
# ---------------------------------------------------------------------------


class TestExtractSessionId:
    """Tests for extracting session ID from tr.dll page HTML."""

    def test_extracts_from_page_html(self) -> None:
        """Session ID should be extracted from tr.dll page HTML."""
        html = (
            "<html><body><script>"
            "var rulingID=10;"
            'var seshID="AA5F410B692A25800E3D6799BA32CDF1E3AB4D5B";'
            "</script></body></html>"
        )
        session_id = extract_session_id(html)
        assert session_id == "AA5F410B692A25800E3D6799BA32CDF1E3AB4D5B"

    def test_extracts_hex_string(self) -> None:
        html = 'var seshID="DEADBEEF1234567890ABCDEF";'
        assert extract_session_id(html) == "DEADBEEF1234567890ABCDEF"

    def test_returns_none_for_missing(self) -> None:
        html = "<html><body>no session here</body></html>"
        assert extract_session_id(html) is None

    def test_returns_none_for_empty(self) -> None:
        assert extract_session_id("") is None


# ---------------------------------------------------------------------------
# Full scraper — mocked HTTP using real fixtures
# ---------------------------------------------------------------------------


def _mock_rest_api(json_text: str) -> None:
    """Mock the REST API endpoint for all RulingIDs.

    The REST API URL pattern is:
      {CIVIL_REST_BASE}//{date}/{session_id}/{ruling_id}

    We use a broad URL pattern match since the date and session ID vary.
    """
    respx.get(url__startswith=CIVIL_REST_BASE).mock(
        return_value=httpx.Response(200, text=json_text),
    )


class TestSFCivilScraperRun:
    """Integration tests for the full scraper run with mocked HTTP.

    All tests inject ``session_id`` to bypass Playwright session acquisition
    and mock the REST API GET endpoint instead of the old POST endpoint.
    """

    @respx.mock
    def test_full_run_success(self) -> None:
        """Full run with one RulingID returning data — should succeed."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        _mock_rest_api(json_text)

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        health = scraper.run()
        assert health.success is True
        # 8 RulingIDs * 12 rulings each = 96
        assert health.records_captured == 8 * 12

    @respx.mock
    def test_fetch_documents_populates_fields(self) -> None:
        """Verify all required fields are populated on fetched documents."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        _mock_rest_api(json_text)

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

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
        _mock_rest_api(json_text)

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        docs = scraper.fetch_documents()
        departments = {d.department for d in docs}
        # All 6 departments should be represented (each RulingID returns data)
        assert departments == {"204", "210", "301", "302", "304", "501"}

    @respx.mock
    def test_fetch_documents_sets_judge_name_from_dept_map(self) -> None:
        """Judge name should be populated from dept_judge_map when provided."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        _mock_rest_api(json_text)

        dept_judge_map = {
            "301": "Curtis E.A. Karnow",
            "302": "Ethan P. Schulman",
            "304": "Anne-Christine Massullo",
            "210": "Samuel K. Feng",
            "501": "Richard B. Ulmer",
            "204": "Probate Judge Name",
        }
        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            dept_judge_map=dept_judge_map,
            session_id=TEST_SESSION_ID,
        )

        docs = scraper.fetch_documents()
        # All documents should have judge names
        has_judge = [d for d in docs if d.judge_name]
        assert len(has_judge) == len(docs)
        # Check specific department -> judge mapping
        dept_301_docs = [d for d in docs if d.department == "301"]
        assert dept_301_docs[0].judge_name == "Curtis E.A. Karnow"

    @respx.mock
    def test_fetch_documents_without_dept_map(self) -> None:
        """Judge name should be None when no dept_judge_map is provided."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        _mock_rest_api(json_text)

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        docs = scraper.fetch_documents()
        # Without dept_judge_map, judge_name should be None
        for doc in docs:
            assert doc.judge_name is None

    @respx.mock
    def test_fetch_documents_sets_parties(self) -> None:
        """Documents with 'VS.' in title should have parties extracted."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        _mock_rest_api(json_text)

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

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
        _mock_rest_api(json_text)

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        docs = scraper.fetch_documents()
        has_motion = [d for d in docs if d.motion_type]
        assert len(has_motion) > 0

    @respx.mock
    def test_fetch_documents_sets_ruling_text_html(self) -> None:
        """ruling_text_html should contain HTML formatting."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        _mock_rest_api(json_text)

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        docs = scraper.fetch_documents()
        has_html = [d for d in docs if d.ruling_text_html]
        assert len(has_html) > 0

    @respx.mock
    def test_fetch_documents_stores_ruling_id_in_extra(self) -> None:
        """Extra metadata should include ruling_id and dept_type."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        _mock_rest_api(json_text)

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        docs = scraper.fetch_documents()
        first = docs[0]
        assert "ruling_id" in first.extra
        assert "dept_type" in first.extra

    @respx.mock
    def test_handles_redirect_gracefully(self) -> None:
        """Redirect during REST fetch should be handled gracefully."""
        respx.get(url__startswith=CIVIL_REST_BASE).mock(
            return_value=httpx.Response(
                302,
                headers={"Location": "https://webapps.sftc.org/tr/captcha.dll"},
            )
        )

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        docs = scraper.fetch_documents()
        assert docs == []

    @respx.mock
    def test_handles_http_error_gracefully(self) -> None:
        """HTTP errors per-RulingID are logged but don't crash the run.

        The scraper catches per-RulingID errors and continues to the next.
        The overall run succeeds with 0 records (no unhandled exception).
        """
        respx.get(url__startswith=CIVIL_REST_BASE).mock(
            return_value=httpx.Response(500),
        )

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        health = scraper.run()
        # Per-RulingID errors are caught — the run itself succeeds
        assert health.success is True
        assert health.records_captured == 0

    @respx.mock
    def test_handles_empty_results(self) -> None:
        """Empty result set should succeed with 0 records."""
        respx.get(url__startswith=CIVIL_REST_BASE).mock(
            return_value=httpx.Response(200, text='{"result": [0, ""]}'),
        )

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        health = scraper.run()
        assert health.success is True
        assert health.records_captured == 0

    @respx.mock
    def test_handles_non_json_response(self) -> None:
        """Non-JSON response should be handled gracefully (no crash)."""
        respx.get(url__startswith=CIVIL_REST_BASE).mock(
            return_value=httpx.Response(200, text="<html>Not JSON</html>"),
        )

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        health = scraper.run()
        assert health.success is True
        assert health.records_captured == 0

    @respx.mock
    def test_handles_session_expiry(self) -> None:
        """Session expiry (result=[−1]) should be handled gracefully."""
        respx.get(url__startswith=CIVIL_REST_BASE).mock(
            return_value=httpx.Response(200, text='{"result": [-1]}'),
        )

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        health = scraper.run()
        assert health.success is True
        assert health.records_captured == 0

    @respx.mock
    def test_content_format_is_html(self) -> None:
        """Documents should have HTML content format."""
        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")
        _mock_rest_api(json_text)

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=TEST_SESSION_ID,
        )

        docs = scraper.fetch_documents()
        for doc in docs:
            assert doc.content_format.value == "html"

    def test_no_session_raises(self) -> None:
        """Session-acquisition failure must raise, not return empty (#2620).

        Previously the scraper logged the failure and returned ``[]``, which
        ``BaseScraper.run()`` treated as a successful zero-records run. That
        reported ``status=success`` in telemetry and masked silent outages.
        The scraper must now raise so the base runner marks the run as
        failed. See :class:`TestRunHealthOnSessionFailure` below for the
        end-to-end ``scraper.run().success is False`` check.
        """
        import unittest.mock

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        # Keep a single attempt so the unit test stays fast; retry behavior
        # is exercised in TestSessionAcquisition.
        config.max_retries = 1
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=None,
        )

        # Patch asyncio.run to return None (simulating failed session acquisition)
        with unittest.mock.patch(
            "asyncio.run",
            return_value=None,
        ):
            with pytest.raises(RuntimeError, match="session acquisition failed"):
                scraper.fetch_documents()


# ---------------------------------------------------------------------------
# Run health on session failure — end-to-end regression for #2620
# ---------------------------------------------------------------------------


class TestRunHealthOnSessionFailure:
    """Regression tests for #2620.

    Previously, when ``_acquire_session`` returned ``None`` after all
    retries, ``fetch_documents`` returned ``[]``. The base runner treated
    that as a completed-without-exception run and recorded
    ``records=0, status=success`` in ``telemetry.scraper_runs``. No alert
    fired, so a silent CAPTCHA-gateway outage could persist indefinitely.

    The fix (#2620): ``fetch_documents`` raises ``RuntimeError`` on
    session-acquisition failure, which propagates through
    ``BaseScraper.run()`` and causes ``success=False`` to be recorded.
    """

    def test_run_success_false_when_acquire_session_returns_none(self) -> None:
        """scraper.run().success is False when _acquire_session returns None.

        This is the explicit acceptance criterion from issue #2620:
        "Add a regression test: when ``_acquire_session`` returns ``None``,
        ``scraper.run().success`` is ``False``."
        """
        import asyncio

        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        # Keep retries at 1 so the test stays fast — the base runner's
        # retry_sync wraps fetch_documents with config.max_retries.
        config.max_retries = 1
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=None,
        )

        async def _mock_acquire_session_returning_none() -> str | None:
            return None

        scraper._acquire_session = _mock_acquire_session_returning_none  # type: ignore[assignment]

        # Guard against the test hanging if asyncio.run is ever reintroduced
        # with a different mock strategy — this path should complete fast.
        health = scraper.run()

        assert health.success is False, (
            "Session-acquisition failure must produce success=False so "
            "telemetry records status=failed instead of silently "
            "reporting records=0 + status=success (#2620)."
        )
        assert health.records_captured == 0
        assert health.error_message is not None
        assert "session" in health.error_message.lower()

        # Reference asyncio to silence lint if the import above is unused
        # in some future refactor — the mock replaces the asyncio.run call
        # site entirely, so the import might otherwise look dead.
        assert asyncio is not None

    def test_run_success_false_when_fetch_documents_raises(self) -> None:
        """Any exception from fetch_documents marks the run failed.

        Belt-and-suspenders check that BaseScraper.run() correctly flips
        success to False when the subclass raises — this is the contract
        the #2620 fix depends on.
        """
        config = sf_civil_default_config()
        config.request_delay_seconds = 0
        config.max_retries = 1
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            session_id=None,
        )

        def _raise_runtime_error() -> list[Any]:
            msg = "simulated session acquisition failure"
            raise RuntimeError(msg)

        scraper.fetch_documents = _raise_runtime_error  # type: ignore[assignment]

        health = scraper.run()
        assert health.success is False
        assert health.records_captured == 0
        assert health.error_message is not None
        assert "simulated session acquisition failure" in health.error_message


# ---------------------------------------------------------------------------
# Session acquisition — mocked Playwright
# ---------------------------------------------------------------------------


class TestSessionAcquisition:
    """Tests for Playwright-based session acquisition with mocks."""

    def test_acquire_session_returns_id_on_success(self) -> None:
        """_acquire_session should return session ID when Turnstile resolves."""
        import asyncio

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        async def _mock_try_acquire(_pw: Any) -> str | None:
            return "DEADBEEF1234567890ABCDEF1234567890ABCDEF"

        scraper._try_acquire_session = _mock_try_acquire  # type: ignore[assignment]
        result = asyncio.run(scraper._acquire_session())
        assert result == "DEADBEEF1234567890ABCDEF1234567890ABCDEF"

    def test_acquire_session_retries_on_failure(self) -> None:
        """_acquire_session should retry and return None after all attempts fail."""
        import asyncio

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        call_count = 0

        async def _mock_try_acquire(_pw: Any) -> str | None:
            nonlocal call_count
            call_count += 1
            return None

        scraper._try_acquire_session = _mock_try_acquire  # type: ignore[assignment]
        result = asyncio.run(scraper._acquire_session())
        assert result is None
        assert call_count == 3  # SESSION_MAX_RETRIES

    def test_acquire_session_retries_on_exception(self) -> None:
        """_acquire_session should retry on exceptions and return None."""
        import asyncio

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        async def _mock_try_acquire(_pw: Any) -> str | None:
            msg = "Browser crashed"
            raise RuntimeError(msg)

        scraper._try_acquire_session = _mock_try_acquire  # type: ignore[assignment]
        result = asyncio.run(scraper._acquire_session())
        assert result is None

    def test_wait_for_session_extracts_id(self) -> None:
        """_wait_for_session should extract seshID from page content."""
        import asyncio
        import unittest.mock

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        mock_page = unittest.mock.AsyncMock()
        # Page URL is not captcha (already redirected)
        mock_page.url = "https://webapps.sftc.org/tr/tr.dll/?RulingID=10"
        # Page content has the seshID
        mock_page.content.return_value = (
            "<script>var rulingID=10;"
            'var seshID="AA5F410B692A25800E3D6799BA32CDF1E3AB4D5B";'
            "</script>"
        )

        result = asyncio.run(scraper._wait_for_session(mock_page))
        assert result == "AA5F410B692A25800E3D6799BA32CDF1E3AB4D5B"

    def test_wait_for_session_returns_none_on_timeout(self) -> None:
        """_wait_for_session should return None when CAPTCHA doesn't resolve."""
        import asyncio
        import unittest.mock

        from courts.ca import sf_civil_tentatives

        # Temporarily reduce timeout for test speed
        original_timeout = sf_civil_tentatives.TURNSTILE_TIMEOUT
        sf_civil_tentatives.TURNSTILE_TIMEOUT = 0.1
        sf_civil_tentatives.TURNSTILE_POLL_INTERVAL = 0.05

        try:
            config = sf_civil_default_config()
            scraper = SFCivilTentativeRulingsScraper(config=config)

            mock_page = unittest.mock.AsyncMock()
            # Page URL is still on captcha (never resolves)
            mock_page.url = "https://webapps.sftc.org/captcha/captcha.dll?referrer=..."
            # Page content has no seshID
            mock_page.content.return_value = "<html>CAPTCHA page</html>"

            result = asyncio.run(scraper._wait_for_session(mock_page))
            assert result is None
        finally:
            sf_civil_tentatives.TURNSTILE_TIMEOUT = original_timeout
            sf_civil_tentatives.TURNSTILE_POLL_INTERVAL = 2.0

    def test_wait_for_session_navigated_but_no_sesh_id(self) -> None:
        """_wait_for_session should poll when page navigates but has no seshID."""
        import asyncio
        import unittest.mock

        from courts.ca import sf_civil_tentatives

        original_timeout = sf_civil_tentatives.TURNSTILE_TIMEOUT
        sf_civil_tentatives.TURNSTILE_TIMEOUT = 0.1
        sf_civil_tentatives.TURNSTILE_POLL_INTERVAL = 0.05

        try:
            config = sf_civil_default_config()
            scraper = SFCivilTentativeRulingsScraper(config=config)

            mock_page = unittest.mock.AsyncMock()
            # URL is NOT captcha (navigated away) but no seshID in content
            mock_page.url = "https://webapps.sftc.org/tr/tr.dll/?RulingID=10"
            mock_page.content.return_value = "<html>No session here</html>"

            result = asyncio.run(scraper._wait_for_session(mock_page))
            assert result is None
        finally:
            sf_civil_tentatives.TURNSTILE_TIMEOUT = original_timeout
            sf_civil_tentatives.TURNSTILE_POLL_INTERVAL = 2.0

    def test_try_acquire_session_with_mocked_playwright(self) -> None:
        """_try_acquire_session should orchestrate browser, context, page, and CAPTCHA."""
        import asyncio
        import unittest.mock

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        # Build a mock Playwright chain:
        # async_playwright() -> pw -> pw.chromium.launch() -> browser
        #   -> browser.new_context() -> context -> context.new_page() -> page
        mock_page = unittest.mock.AsyncMock()
        mock_page.url = "https://webapps.sftc.org/tr/tr.dll/?RulingID=2"
        mock_page.content.return_value = (
            '<script>var rulingID=2;var seshID="CAFE0123456789ABCDEF0123456789ABCAFE0123";</script>'
        )

        mock_context = unittest.mock.AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_browser = unittest.mock.AsyncMock()
        mock_browser.new_context.return_value = mock_context

        mock_pw = unittest.mock.AsyncMock()
        mock_pw.chromium.launch.return_value = mock_browser

        # The async_playwright context manager
        mock_pw_cm = unittest.mock.AsyncMock()
        mock_pw_cm.__aenter__.return_value = mock_pw
        mock_pw_factory = unittest.mock.MagicMock(return_value=mock_pw_cm)

        result = asyncio.run(scraper._try_acquire_session(mock_pw_factory))
        assert result == "CAFE0123456789ABCDEF0123456789ABCAFE0123"
        mock_browser.close.assert_awaited_once()

    def test_fetch_documents_uses_injected_session(self) -> None:
        """fetch_documents should skip Playwright when session_id is provided."""
        import unittest.mock

        json_text = _load_fixture("sf-civil-api-response-rid10-2026-03-23.json")

        with respx.mock:
            _mock_rest_api(json_text)

            config = sf_civil_default_config()
            config.request_delay_seconds = 0
            scraper = SFCivilTentativeRulingsScraper(
                config=config,
                session_id=TEST_SESSION_ID,
            )

            # asyncio.run should NOT be called when session_id is provided
            with unittest.mock.patch("asyncio.run") as mock_run:
                docs = scraper.fetch_documents()
                mock_run.assert_not_called()
                assert len(docs) > 0


class TestApplyStealth:
    """Tests for the _apply_stealth helper."""

    def test_apply_stealth_with_library(self) -> None:
        """_apply_stealth should use playwright-stealth when available."""
        import asyncio
        import unittest.mock

        from courts.ca.sf_civil_tentatives import _apply_stealth

        mock_page = unittest.mock.AsyncMock()
        # playwright-stealth is installed in the dev environment
        asyncio.run(_apply_stealth(mock_page))
        # Should not raise; stealth was applied

    def test_apply_stealth_does_not_raise(self) -> None:
        """_apply_stealth should handle mock page without raising."""
        import asyncio
        import unittest.mock

        from courts.ca.sf_civil_tentatives import _apply_stealth

        # A fresh mock page — stealth applies without error
        mock_page = unittest.mock.AsyncMock()
        mock_page.evaluate = unittest.mock.AsyncMock()
        asyncio.run(_apply_stealth(mock_page))
        # Should complete without raising


# ---------------------------------------------------------------------------
# CAPSolver integration — _try_acquire_session branches on API key
# ---------------------------------------------------------------------------


class TestCAPSolverIntegration:
    """Tests for the CAPSolver-assisted Turnstile session acquisition path.

    ``_try_acquire_session`` branches based on the ``CAPSOLVER_API_KEY``
    environment variable. When the key is present, the scraper invokes
    ``framework.turnstile_solver.solve_turnstile`` to obtain a response
    token and injects it into the gateway page. When the key is absent,
    the scraper falls back to the legacy stealth-polling path.
    """

    def _build_mock_playwright(self) -> tuple[Any, Any, Any]:
        """Build an async_playwright factory with mock page/context/browser.

        Returns:
            A tuple of (mock_pw_factory, mock_page, mock_browser). The
            factory can be passed to ``_try_acquire_session`` and the
            page/browser are exposed so tests can configure URL/content
            and assert on calls.
        """
        import unittest.mock

        mock_page = unittest.mock.AsyncMock()
        mock_page.url = "https://webapps.sftc.org/tr/tr.dll/?RulingID=2"
        mock_page.content.return_value = (
            '<script>var rulingID=2;var seshID="CAFEBABE0123456789ABCDEFCAFEBABE01234567";</script>'
        )

        mock_context = unittest.mock.AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_browser = unittest.mock.AsyncMock()
        mock_browser.new_context.return_value = mock_context

        mock_pw = unittest.mock.AsyncMock()
        mock_pw.chromium.launch.return_value = mock_browser

        mock_pw_cm = unittest.mock.AsyncMock()
        mock_pw_cm.__aenter__.return_value = mock_pw
        mock_pw_factory = unittest.mock.MagicMock(return_value=mock_pw_cm)

        return mock_pw_factory, mock_page, mock_browser

    def test_solver_invoked_when_api_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When CAPSOLVER_API_KEY is set, solve_turnstile is called and token injected."""
        import asyncio
        import unittest.mock

        monkeypatch.setenv(CAPSOLVER_API_KEY_ENV_VAR, "fake-key-xyz")

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        mock_pw_factory, mock_page, mock_browser = self._build_mock_playwright()

        # Patch solve_turnstile in the scraper module's namespace.
        async def _fake_solver(**kwargs: Any) -> str | None:
            assert kwargs["sitekey"] == TURNSTILE_SITEKEY
            assert kwargs["api_key"] == "fake-key-xyz"
            assert "webapps.sftc.org" in kwargs["page_url"]
            return "SOLVED_TOKEN_ABC123"

        with unittest.mock.patch(
            "courts.ca.sf_civil_tentatives.solve_turnstile",
            side_effect=_fake_solver,
        ) as mock_solver:
            result = asyncio.run(scraper._try_acquire_session(mock_pw_factory))

        assert result == "CAFEBABE0123456789ABCDEFCAFEBABE01234567"
        mock_solver.assert_called_once()
        # The token must have been evaluated into the page via evaluate()
        mock_page.evaluate.assert_awaited()
        # evaluate is called with (script, token) — verify token is passed
        call_args = mock_page.evaluate.call_args
        assert call_args.args[1] == "SOLVED_TOKEN_ABC123"
        mock_browser.close.assert_awaited_once()

    def test_solver_skipped_when_api_key_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When CAPSOLVER_API_KEY is unset, solve_turnstile is NOT called."""
        import asyncio
        import unittest.mock

        monkeypatch.delenv(CAPSOLVER_API_KEY_ENV_VAR, raising=False)

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        mock_pw_factory, mock_page, mock_browser = self._build_mock_playwright()

        with unittest.mock.patch("courts.ca.sf_civil_tentatives.solve_turnstile") as mock_solver:
            result = asyncio.run(scraper._try_acquire_session(mock_pw_factory))

        # Stealth fallback still extracts seshID from the mocked page
        assert result == "CAFEBABE0123456789ABCDEFCAFEBABE01234567"
        mock_solver.assert_not_called()
        # evaluate is called by _apply_stealth, but not with our injection
        # script — assert the token injection path was not triggered.
        # We check that no call was made with the token string as 2nd arg.
        for call in mock_page.evaluate.await_args_list:
            if len(call.args) >= 2:
                assert call.args[1] != "SOLVED_TOKEN_ABC123"
        mock_browser.close.assert_awaited_once()

    def test_solver_failure_falls_back_to_stealth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When solve_turnstile returns None, continue to stealth polling."""
        import asyncio
        import unittest.mock

        monkeypatch.setenv(CAPSOLVER_API_KEY_ENV_VAR, "fake-key")

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        mock_pw_factory, _mock_page, mock_browser = self._build_mock_playwright()

        async def _failing_solver(**_kwargs: Any) -> str | None:
            return None

        with unittest.mock.patch(
            "courts.ca.sf_civil_tentatives.solve_turnstile",
            side_effect=_failing_solver,
        ) as mock_solver:
            result = asyncio.run(scraper._try_acquire_session(mock_pw_factory))

        # Solver was called, returned None, but the mocked page still has
        # a seshID so _wait_for_session still succeeds.
        mock_solver.assert_called_once()
        assert result == "CAFEBABE0123456789ABCDEFCAFEBABE01234567"
        mock_browser.close.assert_awaited_once()

    def test_submit_captcha_with_solver_injects_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_submit_captcha_with_solver injects token and invokes callback."""
        import asyncio
        import unittest.mock

        monkeypatch.setenv(CAPSOLVER_API_KEY_ENV_VAR, "fake-key")

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        mock_page = unittest.mock.AsyncMock()

        async def _fake_solver(**_kwargs: Any) -> str | None:
            return "TOKEN_XYZ"

        with unittest.mock.patch(
            "courts.ca.sf_civil_tentatives.solve_turnstile",
            side_effect=_fake_solver,
        ):
            ok = asyncio.run(
                scraper._submit_captcha_with_solver(
                    page=mock_page,
                    captcha_url="https://webapps.sftc.org/captcha/captcha.dll?foo=bar",
                    api_key="fake-key",
                )
            )

        assert ok is True
        mock_page.evaluate.assert_awaited_once()
        # Inspect the call — 1st arg should be the JS snippet, 2nd is the token.
        call_args = mock_page.evaluate.call_args
        assert "recaptchaCallBack" in call_args.args[0]
        assert "g-recaptcha-response" in call_args.args[0]
        assert call_args.args[1] == "TOKEN_XYZ"

    def test_submit_captcha_with_solver_returns_false_on_no_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns False when CAPSolver returns no token; skips injection."""
        import asyncio
        import unittest.mock

        monkeypatch.setenv(CAPSOLVER_API_KEY_ENV_VAR, "fake-key")

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        mock_page = unittest.mock.AsyncMock()

        async def _no_token(**_kwargs: Any) -> str | None:
            return None

        with unittest.mock.patch(
            "courts.ca.sf_civil_tentatives.solve_turnstile",
            side_effect=_no_token,
        ):
            ok = asyncio.run(
                scraper._submit_captcha_with_solver(
                    page=mock_page,
                    captcha_url="https://webapps.sftc.org/captcha/captcha.dll",
                    api_key="fake-key",
                )
            )

        assert ok is False
        mock_page.evaluate.assert_not_awaited()

    def test_submit_captcha_with_solver_returns_false_on_evaluate_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns False when page.evaluate raises (e.g., page navigated)."""
        import asyncio
        import unittest.mock

        monkeypatch.setenv(CAPSOLVER_API_KEY_ENV_VAR, "fake-key")

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        mock_page = unittest.mock.AsyncMock()
        mock_page.evaluate.side_effect = RuntimeError("page closed")

        async def _fake_solver(**_kwargs: Any) -> str | None:
            return "TOKEN_ABC"

        with unittest.mock.patch(
            "courts.ca.sf_civil_tentatives.solve_turnstile",
            side_effect=_fake_solver,
        ):
            ok = asyncio.run(
                scraper._submit_captcha_with_solver(
                    page=mock_page,
                    captcha_url="https://webapps.sftc.org/captcha/captcha.dll",
                    api_key="fake-key",
                )
            )

        assert ok is False


# ---------------------------------------------------------------------------
# Patchright + residential proxy path (#2622)
# ---------------------------------------------------------------------------


class TestProxyInitialization:
    """Tests for ``proxy_url`` / env-var resolution in ``__init__``."""

    def test_proxy_url_from_sf_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When SF_PROXY_URL is set, _proxy_url picks it up."""
        monkeypatch.setenv(SF_PROXY_URL_ENV_VAR, "http://sf-proxy.example:33335")
        monkeypatch.delenv(SD_PROXY_URL_ENV_VAR, raising=False)

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)
        assert scraper._proxy_url == "http://sf-proxy.example:33335"

    def test_proxy_url_from_sd_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When SF_PROXY_URL is unset but SD_PROXY_URL is set, fall back to SD."""
        monkeypatch.delenv(SF_PROXY_URL_ENV_VAR, raising=False)
        monkeypatch.setenv(SD_PROXY_URL_ENV_VAR, "http://sd-proxy.example:33335")

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)
        assert scraper._proxy_url == "http://sd-proxy.example:33335"

    def test_proxy_url_prefers_sf_over_sd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When both SF_PROXY_URL and SD_PROXY_URL are set, SF wins."""
        monkeypatch.setenv(SF_PROXY_URL_ENV_VAR, "http://sf-proxy.example:33335")
        monkeypatch.setenv(SD_PROXY_URL_ENV_VAR, "http://sd-proxy.example:33335")

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)
        assert scraper._proxy_url == "http://sf-proxy.example:33335"

    def test_proxy_url_explicit_arg_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit proxy_url constructor arg takes precedence over env vars."""
        monkeypatch.setenv(SF_PROXY_URL_ENV_VAR, "http://sf-env.example:33335")
        monkeypatch.setenv(SD_PROXY_URL_ENV_VAR, "http://sd-env.example:33335")

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            proxy_url="http://explicit.example:33335",
        )
        assert scraper._proxy_url == "http://explicit.example:33335"

    def test_proxy_url_defaults_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no env vars and no explicit arg, _proxy_url is None."""
        monkeypatch.delenv(SF_PROXY_URL_ENV_VAR, raising=False)
        monkeypatch.delenv(SD_PROXY_URL_ENV_VAR, raising=False)

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)
        assert scraper._proxy_url is None

    def test_empty_env_var_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty string env vars should NOT be passed as a proxy URL."""
        monkeypatch.setenv(SF_PROXY_URL_ENV_VAR, "")
        monkeypatch.setenv(SD_PROXY_URL_ENV_VAR, "")

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)
        assert scraper._proxy_url is None


class TestTurnstileTimeout:
    """TURNSTILE_TIMEOUT guardrail (#2622 bump 45 -> 60)."""

    def test_turnstile_timeout_bumped_to_60(self) -> None:
        """TURNSTILE_TIMEOUT must be at least 60s to accommodate Patchright+proxy."""
        assert TURNSTILE_TIMEOUT >= 60.0


class TestPatchrightFactorySelection:
    """Tests for _acquire_session factory selection (patchright vs playwright)."""

    def test_playwright_used_when_capsolver_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When CAPSOLVER_API_KEY is set, use plain playwright (not patchright)."""
        import asyncio
        import sys
        import unittest.mock

        monkeypatch.setenv(CAPSOLVER_API_KEY_ENV_VAR, "fake")

        # Stub both modules to record which one gets imported.
        fake_playwright = unittest.mock.MagicMock()
        fake_playwright.async_api = unittest.mock.MagicMock()
        fake_playwright.async_api.async_playwright = unittest.mock.MagicMock(
            name="playwright.async_playwright"
        )
        fake_patchright = unittest.mock.MagicMock()
        fake_patchright.async_api = unittest.mock.MagicMock()
        fake_patchright.async_api.async_playwright = unittest.mock.MagicMock(
            name="patchright.async_playwright"
        )

        monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
        monkeypatch.setitem(sys.modules, "playwright.async_api", fake_playwright.async_api)
        monkeypatch.setitem(sys.modules, "patchright", fake_patchright)
        monkeypatch.setitem(sys.modules, "patchright.async_api", fake_patchright.async_api)

        captured: dict[str, Any] = {}

        async def _mock_try_acquire(pw: Any) -> str | None:
            captured["factory"] = pw
            return "DEADBEEF1234567890ABCDEF1234567890ABCDEF"

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)
        scraper._try_acquire_session = _mock_try_acquire  # type: ignore[assignment]

        asyncio.run(scraper._acquire_session())

        assert captured["factory"] is fake_playwright.async_api.async_playwright

    def test_patchright_used_when_capsolver_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When CAPSOLVER_API_KEY is unset, prefer patchright factory."""
        import asyncio
        import sys
        import unittest.mock

        monkeypatch.delenv(CAPSOLVER_API_KEY_ENV_VAR, raising=False)

        fake_playwright = unittest.mock.MagicMock()
        fake_playwright.async_api = unittest.mock.MagicMock()
        fake_playwright.async_api.async_playwright = unittest.mock.MagicMock(
            name="playwright.async_playwright"
        )
        fake_patchright = unittest.mock.MagicMock()
        fake_patchright.async_api = unittest.mock.MagicMock()
        fake_patchright.async_api.async_playwright = unittest.mock.MagicMock(
            name="patchright.async_playwright"
        )

        monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
        monkeypatch.setitem(sys.modules, "playwright.async_api", fake_playwright.async_api)
        monkeypatch.setitem(sys.modules, "patchright", fake_patchright)
        monkeypatch.setitem(sys.modules, "patchright.async_api", fake_patchright.async_api)

        captured: dict[str, Any] = {}

        async def _mock_try_acquire(pw: Any) -> str | None:
            captured["factory"] = pw
            return "DEADBEEF1234567890ABCDEF1234567890ABCDEF"

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)
        scraper._try_acquire_session = _mock_try_acquire  # type: ignore[assignment]

        asyncio.run(scraper._acquire_session())

        assert captured["factory"] is fake_patchright.async_api.async_playwright

    def test_falls_back_to_playwright_when_patchright_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If patchright import fails, fall back to playwright gracefully."""
        import asyncio
        import sys
        import unittest.mock

        monkeypatch.delenv(CAPSOLVER_API_KEY_ENV_VAR, raising=False)

        fake_playwright = unittest.mock.MagicMock()
        fake_playwright.async_api = unittest.mock.MagicMock()
        fake_playwright.async_api.async_playwright = unittest.mock.MagicMock(
            name="playwright.async_playwright"
        )

        # Force patchright import to fail.
        monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
        monkeypatch.setitem(sys.modules, "playwright.async_api", fake_playwright.async_api)
        monkeypatch.setitem(sys.modules, "patchright", None)
        monkeypatch.setitem(sys.modules, "patchright.async_api", None)

        captured: dict[str, Any] = {}

        async def _mock_try_acquire(pw: Any) -> str | None:
            captured["factory"] = pw
            return "DEADBEEF1234567890ABCDEF1234567890ABCDEF"

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)
        scraper._try_acquire_session = _mock_try_acquire  # type: ignore[assignment]

        asyncio.run(scraper._acquire_session())

        assert captured["factory"] is fake_playwright.async_api.async_playwright


class TestProxyPassedToLaunch:
    """Tests for ``_try_acquire_session`` passing proxy to browser launch."""

    def _build_mock_playwright(self, factory_module: str = "unittest.mock") -> tuple[Any, Any, Any]:
        """Build an async_playwright mock; set __module__ to simulate patchright."""
        import unittest.mock

        mock_page = unittest.mock.AsyncMock()
        mock_page.url = "https://webapps.sftc.org/tr/tr.dll/?RulingID=2"
        mock_page.content.return_value = (
            '<script>var seshID="CAFEBABE0123456789ABCDEFCAFEBABE01234567";</script>'
        )

        mock_context = unittest.mock.AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_browser = unittest.mock.AsyncMock()
        mock_browser.new_context.return_value = mock_context

        mock_pw = unittest.mock.AsyncMock()
        mock_pw.chromium.launch.return_value = mock_browser

        mock_pw_cm = unittest.mock.AsyncMock()
        mock_pw_cm.__aenter__.return_value = mock_pw

        mock_pw_factory = unittest.mock.MagicMock(return_value=mock_pw_cm)
        # Override __module__ so the scraper's is_patchright detection works.
        mock_pw_factory.__module__ = factory_module

        return mock_pw_factory, mock_pw, mock_browser

    def test_proxy_passed_to_launch_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When _proxy_url is set, launch receives proxy={"server": url}."""
        import asyncio

        monkeypatch.delenv(CAPSOLVER_API_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv(SF_PROXY_URL_ENV_VAR, raising=False)
        monkeypatch.delenv(SD_PROXY_URL_ENV_VAR, raising=False)

        mock_pw_factory, mock_pw, _ = self._build_mock_playwright()

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            proxy_url="http://proxy.example:33335",
        )

        asyncio.run(scraper._try_acquire_session(mock_pw_factory))

        launch_call = mock_pw.chromium.launch.call_args
        assert launch_call.kwargs.get("proxy") == {"server": "http://proxy.example:33335"}

    def test_proxy_not_passed_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When _proxy_url is None, launch kwargs do not include 'proxy'."""
        import asyncio

        monkeypatch.delenv(CAPSOLVER_API_KEY_ENV_VAR, raising=False)
        monkeypatch.delenv(SF_PROXY_URL_ENV_VAR, raising=False)
        monkeypatch.delenv(SD_PROXY_URL_ENV_VAR, raising=False)

        mock_pw_factory, mock_pw, _ = self._build_mock_playwright()

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)
        assert scraper._proxy_url is None

        asyncio.run(scraper._try_acquire_session(mock_pw_factory))

        launch_call = mock_pw.chromium.launch.call_args
        assert "proxy" not in launch_call.kwargs

    def test_patchright_path_uses_minimal_launch_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On the patchright path, launch uses channel=chrome and no custom args."""
        import asyncio

        monkeypatch.delenv(CAPSOLVER_API_KEY_ENV_VAR, raising=False)

        mock_pw_factory, mock_pw, _ = self._build_mock_playwright(
            factory_module="patchright.async_api"
        )

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(
            config=config,
            proxy_url="http://proxy.example:33335",
        )

        asyncio.run(scraper._try_acquire_session(mock_pw_factory))

        launch_call = mock_pw.chromium.launch.call_args
        assert launch_call.kwargs.get("channel") == "chrome"
        # Zero custom args on patchright path
        assert "args" not in launch_call.kwargs
        # Proxy still flows through
        assert launch_call.kwargs.get("proxy") == {"server": "http://proxy.example:33335"}

    def test_patchright_path_uses_bare_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On patchright path, new_context is called with no overrides."""
        import asyncio

        monkeypatch.delenv(CAPSOLVER_API_KEY_ENV_VAR, raising=False)

        mock_pw_factory, mock_pw, mock_browser = self._build_mock_playwright(
            factory_module="patchright.async_api"
        )

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        asyncio.run(scraper._try_acquire_session(mock_pw_factory))

        context_call = mock_browser.new_context.call_args
        # Bare context: no user_agent, viewport, timezone, locale overrides
        assert context_call.kwargs == {}

    def test_patchright_path_skips_apply_stealth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On patchright path, _apply_stealth is NOT invoked on the page."""
        import asyncio
        import unittest.mock

        monkeypatch.delenv(CAPSOLVER_API_KEY_ENV_VAR, raising=False)

        mock_pw_factory, _, _ = self._build_mock_playwright(factory_module="patchright.async_api")

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        with unittest.mock.patch("courts.ca.sf_civil_tentatives._apply_stealth") as mock_apply:
            asyncio.run(scraper._try_acquire_session(mock_pw_factory))

        mock_apply.assert_not_called()

    def test_playwright_path_still_applies_stealth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On legacy playwright path, _apply_stealth IS invoked."""
        import asyncio
        import unittest.mock

        monkeypatch.delenv(CAPSOLVER_API_KEY_ENV_VAR, raising=False)

        # factory_module="unittest.mock" => is_patchright=False (legacy path)
        mock_pw_factory, _, _ = self._build_mock_playwright()

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        with unittest.mock.patch("courts.ca.sf_civil_tentatives._apply_stealth") as mock_apply:
            asyncio.run(scraper._try_acquire_session(mock_pw_factory))

        mock_apply.assert_awaited_once()

    def test_capsolver_path_still_uses_full_launch_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On CAPSolver path (patchright factory or not), full args are used."""
        import asyncio
        import unittest.mock

        monkeypatch.setenv(CAPSOLVER_API_KEY_ENV_VAR, "fake-key")

        # Even if factory claims to be patchright, CAPSolver path wins.
        mock_pw_factory, mock_pw, _ = self._build_mock_playwright(
            factory_module="patchright.async_api"
        )

        async def _fake_solver(**_kwargs: Any) -> str | None:
            return "TOKEN"

        config = sf_civil_default_config()
        scraper = SFCivilTentativeRulingsScraper(config=config)

        with unittest.mock.patch(
            "courts.ca.sf_civil_tentatives.solve_turnstile",
            side_effect=_fake_solver,
        ):
            asyncio.run(scraper._try_acquire_session(mock_pw_factory))

        launch_call = mock_pw.chromium.launch.call_args
        # CAPSolver path uses legacy args
        assert "args" in launch_call.kwargs
        assert "channel" not in launch_call.kwargs


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
