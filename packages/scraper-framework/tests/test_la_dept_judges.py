"""Tests for LA department-to-judge mapping — built against real-format fixture HTML.

Fixtures:
    la_judicial_officers.html — representative HTML table matching the structure
    of https://www.lacourt.ca.gov/judicialofficers/ui/SearchResult.aspx
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.la_dept_judges import (
    JUDICIAL_OFFICERS_URL,
    JudicialOfficer,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    normalize_department,
    parse_judicial_officers_html,
)
from courts.ca.la_tentatives import LATentativeRulingsScraper, default_config
from framework import ContentFormat
from framework.models import CapturedDocument

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# normalize_department — unit tests
# ---------------------------------------------------------------------------


class TestNormalizeDepartment:
    def test_strips_leading_zeros(self) -> None:
        assert normalize_department("052") == "52"

    def test_strips_leading_zeros_single_digit(self) -> None:
        assert normalize_department("005") == "5"

    def test_no_change_for_unpadded(self) -> None:
        assert normalize_department("3") == "3"

    def test_alphanumeric_unchanged(self) -> None:
        assert normalize_department("F46") == "F46"

    def test_alpha_only_unchanged(self) -> None:
        assert normalize_department("H") == "H"

    def test_zero_stays_zero(self) -> None:
        assert normalize_department("0") == "0"

    def test_whitespace_stripped(self) -> None:
        assert normalize_department(" F46 ") == "F46"


# ---------------------------------------------------------------------------
# parse_judicial_officers_html — against fixture
# ---------------------------------------------------------------------------


class TestParseJudicialOfficersHtml:
    def test_parses_all_officers(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        assert len(officers) == 14

    def test_first_officer_fields(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        first = officers[0]
        assert first.last_name == "Abeles"
        assert first.first_name == "Jerrold"
        assert first.title == "Judge"
        assert first.courthouse == "Stanley Mosk"
        assert first.department == "052"
        assert first.primary_assignment == "Civil"

    def test_full_name_property(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        first = officers[0]
        assert first.full_name == "Jerrold Abeles"

    def test_commissioner_found(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        commissioners = [o for o in officers if o.title == "Commissioner"]
        assert len(commissioners) >= 1

    def test_empty_table_returns_empty(self) -> None:
        html = (
            "<html><body><table id='GridView1'>"
            "<thead><tr><th>A</th></tr></thead>"
            "</table></body></html>"
        )
        officers = parse_judicial_officers_html(html)
        assert officers == []

    def test_no_table_returns_empty(self) -> None:
        html = "<html><body><p>No table here</p></body></html>"
        officers = parse_judicial_officers_html(html)
        assert officers == []

    def test_crowfoot_in_alhambra(self) -> None:
        """Crowfoot should be in Alhambra Courthouse Dept 3 — matches LA scraper fixture."""
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        crowfoot = [o for o in officers if o.last_name == "Crowfoot"]
        assert len(crowfoot) == 1
        assert crowfoot[0].department == "3"
        assert crowfoot[0].courthouse == "Alhambra Courthouse"


# ---------------------------------------------------------------------------
# build_department_judge_map — unit tests
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_fixture(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(officers)
        assert len(dept_map) == 14

    def test_normalizes_zero_padded_depts(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(officers)
        # "052" in the table should be accessible as "52"
        assert "52" in dept_map
        assert dept_map["52"] == "Jerrold Abeles"

    def test_alphanumeric_dept_preserved(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(officers)
        assert "F46" in dept_map
        assert dept_map["F46"] == "Kerry Duffy-Lewis"

    def test_single_char_dept(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(officers)
        assert "H" in dept_map
        assert dept_map["H"] == "Maria Elena Garcia"

    def test_duplicate_dept_keeps_first(self) -> None:
        """If two officers share a dept, the first wins."""
        officers = [
            JudicialOfficer(
                last_name="Smith",
                first_name="John",
                title="Judge",
                courthouse="Test",
                department="10",
                phone="",
                primary_assignment="Civil",
            ),
            JudicialOfficer(
                last_name="Jones",
                first_name="Jane",
                title="Judge",
                courthouse="Test",
                department="10",
                phone="",
                primary_assignment="Civil",
            ),
        ]
        dept_map = build_department_judge_map(officers)
        assert dept_map["10"] == "John Smith"


# ---------------------------------------------------------------------------
# lookup_judge_for_department — unit tests
# ---------------------------------------------------------------------------


class TestLookupJudgeForDepartment:
    def test_found_with_normalized_key(self) -> None:
        dept_map = {"52": "Jerrold Abeles", "3": "William A. Crowfoot"}
        assert lookup_judge_for_department(dept_map, "52") == "Jerrold Abeles"

    def test_found_with_padded_input(self) -> None:
        """Lookup normalizes the input department too."""
        dept_map = {"52": "Jerrold Abeles"}
        assert lookup_judge_for_department(dept_map, "052") == "Jerrold Abeles"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"52": "Jerrold Abeles"}
        assert lookup_judge_for_department(dept_map, "99") is None

    def test_alpha_dept_found(self) -> None:
        dept_map = {"F46": "Kerry Duffy-Lewis"}
        assert lookup_judge_for_department(dept_map, "F46") == "Kerry Duffy-Lewis"


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_mapping_with_fixture() -> None:
    """fetch_department_judge_mapping returns a correct map from fixture HTML."""
    html = _load("la_judicial_officers.html")
    respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))
    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) == 14
    assert dept_map["3"] == "William A. Crowfoot"
    assert dept_map["52"] == "Jerrold Abeles"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# Integration: LA scraper should use mapping to populate judge_name
# ---------------------------------------------------------------------------


def test_mapping_matches_la_scraper_departments() -> None:
    """Verify the mapping covers departments seen in the LA scraper fixtures.

    The LA scraper fixture uses Alhambra Courthouse Dept 3, which should
    map to William A. Crowfoot in our judicial officers fixture.
    """
    html = _load("la_judicial_officers.html")
    officers = parse_judicial_officers_html(html)
    dept_map = build_department_judge_map(officers)

    # Dept 3 (Alhambra) — from la_ruling_response.html fixture
    assert lookup_judge_for_department(dept_map, "3") == "William A. Crowfoot"

    # Dept F46 (Chatsworth) — from la_ruling_cha_f46.html fixture
    assert lookup_judge_for_department(dept_map, "F46") == "Kerry Duffy-Lewis"

    # Dept A (Compton) — from la_ruling_com_a.html fixture
    assert lookup_judge_for_department(dept_map, "A") == "Jared D. Moses"

    # Dept 205 (Beverly Hills) — from la_ruling_bh205.html fixture
    assert lookup_judge_for_department(dept_map, "205") == "Helen I. Kim"

    # Dept P (Pasadena) — from la_ruling_pas_p.html fixture
    assert lookup_judge_for_department(dept_map, "P") == "Daniel J. Palazuelos"


# ---------------------------------------------------------------------------
# Scraper integration: parse_document fallback from dept_judge_map
# ---------------------------------------------------------------------------


class TestScraperDeptJudgeMapFallback:
    """Test that LATentativeRulingsScraper.parse_document uses the dept-judge
    mapping as a fallback when the ruling text doesn't contain a judge name."""

    def _make_doc_without_judge(self, department: str = "52") -> CapturedDocument:
        """Create a ruling doc whose HTML has no judge name signature."""
        html = (
            '<html><body><div id="speechSynthesis">'
            "<p><B>Case Number:</B> 24STCV12345</p>"
            "<p>The motion for summary judgment is GRANTED.</p>"
            "</div></body></html>"
        )
        from datetime import datetime

        return CapturedDocument(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=datetime(2026, 3, 2, 18, 0, 0),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="",
            department=department,
        )

    def test_populates_judge_from_mapping_when_text_has_none(self) -> None:
        """When ruling text lacks a judge name, use dept mapping."""
        dept_map = {"52": "Jerrold Abeles", "3": "William A. Crowfoot"}
        config = default_config()
        scraper = LATentativeRulingsScraper(config=config, dept_judge_map=dept_map)

        doc = self._make_doc_without_judge(department="52")
        result = scraper.parse_document(doc)
        assert result.judge_name == "Jerrold Abeles"

    def test_does_not_override_judge_from_text(self) -> None:
        """When ruling text contains a judge name, don't override with mapping."""
        html = (
            '<html><body><div id="speechSynthesis">'
            "<p><B>Case Number:</B> 24STCV12345</p>"
            "<p>The motion is GRANTED.</p>"
            "<div>Sarah Johnson Judge of the Superior Court</div>"
            "</div></body></html>"
        )
        from datetime import datetime

        doc = CapturedDocument(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=datetime(2026, 3, 2, 18, 0, 0),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="",
            department="52",
        )

        dept_map = {"52": "Jerrold Abeles"}
        config = default_config()
        scraper = LATentativeRulingsScraper(config=config, dept_judge_map=dept_map)

        result = scraper.parse_document(doc)
        assert result.judge_name == "Sarah Johnson"

    def test_no_mapping_leaves_judge_none(self) -> None:
        """Without a dept_judge_map, judge_name stays None."""
        config = default_config()
        scraper = LATentativeRulingsScraper(config=config)

        doc = self._make_doc_without_judge(department="52")
        result = scraper.parse_document(doc)
        assert result.judge_name is None

    def test_unknown_dept_leaves_judge_none(self) -> None:
        """If department not in mapping, judge_name stays None."""
        dept_map = {"3": "William A. Crowfoot"}
        config = default_config()
        scraper = LATentativeRulingsScraper(config=config, dept_judge_map=dept_map)

        doc = self._make_doc_without_judge(department="999")
        result = scraper.parse_document(doc)
        assert result.judge_name is None
