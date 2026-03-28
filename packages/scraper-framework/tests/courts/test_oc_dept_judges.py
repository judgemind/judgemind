"""Tests for Orange County department-to-judge mapping — built against real fixtures.

Fixtures:
    oc_judicial_officers.html — OC judicial officers page
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from courts.ca.la_dept_judges import normalize_department
from courts.ca.oc_dept_judges import (
    JUDICIAL_OFFICERS_URL,
    JudicialOfficer,
    OCCourtDirectory,
    _parse_last_first_name,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    parse_judicial_officers_html,
)

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _parse_last_first_name — unit tests
# ---------------------------------------------------------------------------


class TestParseLastFirstName:
    def test_standard_name(self) -> None:
        assert _parse_last_first_name("ADAMS, JOHN S.") == "John S. Adams"

    def test_hyphenated_last_name(self) -> None:
        assert _parse_last_first_name("FLYNN-PEISTER, TERRI") == "Terri Flynn-Peister"

    def test_multi_word_last_name(self) -> None:
        assert _parse_last_first_name("DE LA TORRE, MARICELA") == "Maricela De La Torre"

    def test_no_comma_returns_title_case(self) -> None:
        assert _parse_last_first_name("SMITH") == "Smith"

    def test_empty_string(self) -> None:
        assert _parse_last_first_name("") == ""


# ---------------------------------------------------------------------------
# parse_judicial_officers_html — fixture tests
# ---------------------------------------------------------------------------


class TestParseJudicialOfficers:
    def test_parses_100_plus_officers(self) -> None:
        html = _load("oc_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        assert len(officers) >= 100

    def test_harbor_panel_officer(self) -> None:
        html = _load("oc_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        h06 = [o for o in officers if normalize_department(o.department) == "H06"]
        assert len(h06) == 1
        assert h06[0].name == "John S. Adams"
        assert h06[0].panel == "Harbor Panel"

    def test_west_panel_officer(self) -> None:
        html = _load("oc_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        w16 = [o for o in officers if normalize_department(o.department) == "W16"]
        assert len(w16) == 1
        assert w16[0].name == "Claudia C. Alvarez"
        assert w16[0].panel == "West Panel"

    def test_phone_has_no_nbsp(self) -> None:
        """Phone numbers should have regular spaces, not non-breaking spaces."""
        html = _load("oc_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        for officer in officers:
            assert "\xa0" not in officer.phone

    def test_names_are_title_case(self) -> None:
        """Names should be in First Last format, not ALL CAPS."""
        html = _load("oc_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        for officer in officers[:10]:
            assert officer.name != officer.name.upper()

    def test_department_codes_present(self) -> None:
        """Every officer should have a department code."""
        html = _load("oc_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        for officer in officers:
            assert officer.department, f"Missing department for {officer.name}"


# ---------------------------------------------------------------------------
# parse_judicial_officers_html — edge cases
# ---------------------------------------------------------------------------


class TestParseEdgeCases:
    def test_empty_html_returns_empty(self) -> None:
        officers = parse_judicial_officers_html("<html><body></body></html>")
        assert officers == []

    def test_no_table_returns_empty(self) -> None:
        officers = parse_judicial_officers_html("<html><body><p>No table</p></body></html>")
        assert officers == []

    def test_table_without_required_columns_returns_empty(self) -> None:
        html = """<html><body>
        <table>
        <tr><th>Name</th><th>Title</th></tr>
        <tr><td>John</td><td>Judge</td></tr>
        </table></body></html>"""
        officers = parse_judicial_officers_html(html)
        assert officers == []


# ---------------------------------------------------------------------------
# build_department_judge_map — unit tests
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_fixture(self) -> None:
        html = _load("oc_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(officers)
        assert len(dept_map) >= 100

    def test_department_codes_preserved(self) -> None:
        html = _load("oc_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(officers)
        assert "H06" in dept_map
        assert dept_map["H06"] == "John S. Adams"

    def test_duplicate_keeps_first(self) -> None:
        officers = [
            JudicialOfficer(name="First Judge", panel="", floor="", department="H06", phone=""),
            JudicialOfficer(name="Second Judge", panel="", floor="", department="H06", phone=""),
        ]
        dept_map = build_department_judge_map(officers)
        assert dept_map["H06"] == "First Judge"


# ---------------------------------------------------------------------------
# lookup_judge_for_department — unit tests
# ---------------------------------------------------------------------------


class TestLookupJudgeForDepartment:
    def test_found(self) -> None:
        dept_map = {"H06": "John S. Adams", "W16": "Claudia C. Alvarez"}
        assert lookup_judge_for_department(dept_map, "H06") == "John S. Adams"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"H06": "John S. Adams"}
        assert lookup_judge_for_department(dept_map, "Z99") is None


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_mapping_with_fixture() -> None:
    """fetch_department_judge_mapping returns a correct map from fixture HTML."""
    html = _load("oc_judicial_officers.html")
    respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))
    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) >= 100
    assert dept_map["H06"] == "John S. Adams"
    assert dept_map["W16"] == "Claudia C. Alvarez"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# OCCourtDirectory — tests
# ---------------------------------------------------------------------------


class TestOCCourtDirectory:
    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        html = _load("oc_judicial_officers.html")
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))

        directory = OCCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=MagicMock(),
        )
        raw, mapping = directory.fetch_current()

        assert isinstance(raw, bytes)
        assert len(raw) > 0
        assert len(mapping) >= 100
        assert mapping["H06"] == "John S. Adams"

    def test_court_id(self) -> None:
        assert OCCourtDirectory.COURT_ID == "ca_orange"

    @respx.mock
    def test_fetch_and_snapshot_defaults_court_id(self) -> None:
        html = _load("oc_judicial_officers.html")
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None

        directory = OCCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=mock_conn,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) >= 100
        assert mapping["H06"] == "John S. Adams"
