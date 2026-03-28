"""Tests for Fresno County department-to-judge mapping — built against real fixtures.

Fixtures:
    fresno_judicial_assignments.html — Fresno judicial assignments page (5 tables)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from courts.ca.fresno_dept_judges import (
    JUDICIAL_ASSIGNMENTS_URL,
    DepartmentJudge,
    FresnoCourtDirectory,
    _normalize_fresno_department,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    parse_judicial_assignments_html,
)

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _normalize_fresno_department — unit tests
# ---------------------------------------------------------------------------


class TestNormalizeFresnoDepartment:
    def test_dept_prefix(self) -> None:
        assert _normalize_fresno_department("Dept. 1") == "1"

    def test_dept_prefix_multidigit(self) -> None:
        assert _normalize_fresno_department("Dept. 201") == "201"

    def test_dept_prefix_alphanumeric(self) -> None:
        assert _normalize_fresno_department("Dept. 97A") == "97A"

    def test_already_normalized(self) -> None:
        assert _normalize_fresno_department("1") == "1"


# ---------------------------------------------------------------------------
# parse_judicial_assignments_html — fixture tests
# ---------------------------------------------------------------------------


class TestParseJudicialAssignments:
    def test_parses_50_plus_officers(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        entries = parse_judicial_assignments_html(html)
        assert len(entries) >= 50

    def test_downtown_officer(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        entries = parse_judicial_assignments_html(html)
        dept1 = [e for e in entries if _normalize_fresno_department(e.department) == "1"]
        assert len(dept1) == 1
        assert dept1[0].judge_name == "Melissa B. Baloian"
        assert dept1[0].phone == "457-6331"

    def test_sisk_court_officer(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        entries = parse_judicial_assignments_html(html)
        dept201 = [e for e in entries if _normalize_fresno_department(e.department) == "201"]
        assert len(dept201) == 1
        assert dept201[0].judge_name == "Pahoua C. Lor"

    def test_juvenile_officer(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        entries = parse_judicial_assignments_html(html)
        dept99a = [e for e in entries if _normalize_fresno_department(e.department) == "99A"]
        assert len(dept99a) == 1
        assert dept99a[0].judge_name == "Amythest Freeman"

    def test_m_street_officer(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        entries = parse_judicial_assignments_html(html)
        dept97a = [e for e in entries if _normalize_fresno_department(e.department) == "97A"]
        assert len(dept97a) == 1
        assert dept97a[0].judge_name == "Irene Luna"

    def test_all_entries_have_phone(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        entries = parse_judicial_assignments_html(html)
        # Most entries should have phone numbers (some may be empty for jail annex)
        with_phone = [e for e in entries if e.phone]
        assert len(with_phone) >= 45

    def test_all_entries_have_assignment(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        entries = parse_judicial_assignments_html(html)
        with_assignment = [e for e in entries if e.assignment]
        assert len(with_assignment) >= 50


# ---------------------------------------------------------------------------
# parse_judicial_assignments_html — edge cases
# ---------------------------------------------------------------------------


class TestParseEdgeCases:
    def test_empty_html_returns_empty(self) -> None:
        entries = parse_judicial_assignments_html("<html><body></body></html>")
        assert entries == []

    def test_no_tables_returns_empty(self) -> None:
        entries = parse_judicial_assignments_html("<html><body><p>No tables</p></body></html>")
        assert entries == []

    def test_table_without_required_columns_returns_empty(self) -> None:
        html = """<html><body>
        <table>
        <tr><th>Name</th><th>Title</th></tr>
        <tr><td>John</td><td>Judge</td></tr>
        </table></body></html>"""
        entries = parse_judicial_assignments_html(html)
        assert entries == []


# ---------------------------------------------------------------------------
# build_department_judge_map — unit tests
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_fixture(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        entries = parse_judicial_assignments_html(html)
        dept_map = build_department_judge_map(entries)
        assert len(dept_map) >= 50

    def test_strips_dept_prefix(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        entries = parse_judicial_assignments_html(html)
        dept_map = build_department_judge_map(entries)
        assert "1" in dept_map
        assert dept_map["1"] == "Melissa B. Baloian"

    def test_preserves_alphanumeric(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        entries = parse_judicial_assignments_html(html)
        dept_map = build_department_judge_map(entries)
        assert "97A" in dept_map
        assert dept_map["97A"] == "Irene Luna"

    def test_duplicate_keeps_first(self) -> None:
        entries = [
            DepartmentJudge(
                department="Dept. 1",
                judge_name="First Judge",
                phone="",
                assignment="",
                courthouse="",
            ),
            DepartmentJudge(
                department="Dept. 1",
                judge_name="Second Judge",
                phone="",
                assignment="",
                courthouse="",
            ),
        ]
        dept_map = build_department_judge_map(entries)
        assert dept_map["1"] == "First Judge"


# ---------------------------------------------------------------------------
# lookup_judge_for_department — unit tests
# ---------------------------------------------------------------------------


class TestLookupJudgeForDepartment:
    def test_found_with_dept_prefix(self) -> None:
        dept_map = {"1": "Melissa B. Baloian"}
        assert lookup_judge_for_department(dept_map, "Dept. 1") == "Melissa B. Baloian"

    def test_found_with_raw_number(self) -> None:
        dept_map = {"1": "Melissa B. Baloian"}
        assert lookup_judge_for_department(dept_map, "1") == "Melissa B. Baloian"

    def test_found_alphanumeric(self) -> None:
        dept_map = {"97A": "Irene Luna"}
        assert lookup_judge_for_department(dept_map, "Dept. 97A") == "Irene Luna"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"1": "Melissa B. Baloian"}
        assert lookup_judge_for_department(dept_map, "999") is None


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_mapping_with_fixture() -> None:
    """fetch_department_judge_mapping returns a correct map from fixture HTML."""
    html = _load("fresno_judicial_assignments.html")
    respx.get(JUDICIAL_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=html))
    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) >= 50
    assert dept_map["1"] == "Melissa B. Baloian"
    assert dept_map["201"] == "Pahoua C. Lor"
    assert dept_map["99A"] == "Amythest Freeman"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(JUDICIAL_ASSIGNMENTS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# FresnoCourtDirectory — tests
# ---------------------------------------------------------------------------


class TestFresnoCourtDirectory:
    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        respx.get(JUDICIAL_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=html))

        directory = FresnoCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=MagicMock(),
        )
        raw, mapping = directory.fetch_current()

        assert isinstance(raw, bytes)
        assert len(raw) > 0
        assert len(mapping) >= 50
        assert mapping["1"] == "Melissa B. Baloian"

    def test_court_id(self) -> None:
        assert FresnoCourtDirectory.COURT_ID == "ca_fresno"

    @respx.mock
    def test_fetch_and_snapshot_defaults_court_id(self) -> None:
        html = _load("fresno_judicial_assignments.html")
        respx.get(JUDICIAL_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=html))

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None

        directory = FresnoCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=mock_conn,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) >= 50
        assert mapping["1"] == "Melissa B. Baloian"
