"""Tests for Kern County department-to-judge mapping — built against real fixtures.

Fixtures:
    kern_judicial_officers.html — Kern judicial officers page (9 tables)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from courts.ca.kern_dept_judges import (
    JUDICIAL_OFFICERS_URL,
    DepartmentJudge,
    KernCourtDirectory,
    _normalize_kern_department,
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
# _normalize_kern_department — unit tests
# ---------------------------------------------------------------------------


class TestNormalizeKernDepartment:
    def test_dept_prefix(self) -> None:
        assert _normalize_kern_department("Dept. 1") == "1"

    def test_dept_prefix_multidigit(self) -> None:
        assert _normalize_kern_department("Dept. 15") == "15"

    def test_div_prefix(self) -> None:
        assert _normalize_kern_department("Div. A") == "A"

    def test_juvenile_code(self) -> None:
        assert _normalize_kern_department("J1") == "J1"

    def test_traffic_code(self) -> None:
        assert _normalize_kern_department("T1") == "T1"

    def test_satellite_code(self) -> None:
        assert _normalize_kern_department("Delano A") == "Delano A"

    def test_presiding(self) -> None:
        assert _normalize_kern_department("Presiding Department") == "Presiding Department"


# ---------------------------------------------------------------------------
# parse_judicial_officers_html — fixture tests
# ---------------------------------------------------------------------------


class TestParseJudicialOfficers:
    def test_parses_40_plus_officers(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        assert len(entries) >= 40

    def test_metropolitan_officer(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        metro = [e for e in entries if e.courthouse == "Metropolitan Division"]
        assert len(metro) >= 15
        dept1 = [e for e in metro if e.department == "Dept. 1"]
        assert len(dept1) == 1
        assert dept1[0].judge_name == "Tiffany Organ-Bowles"

    def test_justice_building_officer(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        jb = [e for e in entries if "Justice Building" in e.courthouse]
        assert len(jb) >= 8
        div_a = [e for e in jb if e.department == "Div. A"]
        assert len(div_a) == 1
        assert div_a[0].judge_name == "Samantha Allen"

    def test_satellite_courthouse(self) -> None:
        """Entries from at least one satellite courthouse should be present."""
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        satellite_courthouses = {"Delano", "Shafter", "Lamont", "Mojave", "Ridgecrest"}
        satellite_entries = [e for e in entries if e.courthouse in satellite_courthouses]
        assert len(satellite_entries) >= 3

    def test_delano_officer(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        delano = [e for e in entries if e.courthouse == "Delano"]
        assert len(delano) >= 1
        assert any(e.judge_name == "Jose R. Benavides" for e in delano)

    def test_skips_vacant_entries(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        names = [e.judge_name.lower() for e in entries]
        assert "vacant" not in names

    def test_skips_under_construction(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        names = [e.judge_name.lower() for e in entries]
        assert "under construction" not in names

    def test_juvenile_justice_center(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        juv = [e for e in entries if e.courthouse == "Juvenile Justice Center"]
        assert len(juv) >= 2
        j1 = [e for e in juv if e.department == "J1"]
        assert len(j1) == 1
        assert j1[0].judge_name == "James Green"


# ---------------------------------------------------------------------------
# parse_judicial_officers_html — edge cases
# ---------------------------------------------------------------------------


class TestParseEdgeCases:
    def test_empty_html_returns_empty(self) -> None:
        entries = parse_judicial_officers_html("<html><body></body></html>")
        assert entries == []

    def test_no_tables_returns_empty(self) -> None:
        entries = parse_judicial_officers_html("<html><body><p>No tables</p></body></html>")
        assert entries == []


# ---------------------------------------------------------------------------
# build_department_judge_map — unit tests
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_fixture(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(entries)
        assert len(dept_map) >= 40

    def test_normalizes_dept_prefix(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(entries)
        assert "1" in dept_map
        assert dept_map["1"] == "Tiffany Organ-Bowles"

    def test_preserves_div_codes(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(entries)
        assert "A" in dept_map
        assert dept_map["A"] == "Samantha Allen"

    def test_preserves_juvenile_codes(self) -> None:
        html = _load("kern_judicial_officers.html")
        entries = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(entries)
        assert "J1" in dept_map
        assert dept_map["J1"] == "James Green"

    def test_duplicate_keeps_first(self) -> None:
        entries = [
            DepartmentJudge(
                department="Dept. 1",
                judge_name="First Judge",
                assignment="",
                courthouse="",
            ),
            DepartmentJudge(
                department="Dept. 1",
                judge_name="Second Judge",
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
        dept_map = {"1": "Tiffany Organ-Bowles"}
        assert lookup_judge_for_department(dept_map, "Dept. 1") == "Tiffany Organ-Bowles"

    def test_found_with_raw_number(self) -> None:
        dept_map = {"1": "Tiffany Organ-Bowles"}
        assert lookup_judge_for_department(dept_map, "1") == "Tiffany Organ-Bowles"

    def test_found_div_prefix(self) -> None:
        dept_map = {"A": "Samantha Allen"}
        assert lookup_judge_for_department(dept_map, "Div. A") == "Samantha Allen"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"1": "Tiffany Organ-Bowles"}
        assert lookup_judge_for_department(dept_map, "99") is None


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_mapping_with_fixture() -> None:
    """fetch_department_judge_mapping returns a correct map from fixture HTML."""
    html = _load("kern_judicial_officers.html")
    respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))
    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) >= 40
    assert dept_map["1"] == "Tiffany Organ-Bowles"
    assert dept_map["A"] == "Samantha Allen"
    assert dept_map["J1"] == "James Green"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# KernCourtDirectory — tests
# ---------------------------------------------------------------------------


class TestKernCourtDirectory:
    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        html = _load("kern_judicial_officers.html")
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))

        directory = KernCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=MagicMock(),
        )
        raw, mapping = directory.fetch_current()

        assert isinstance(raw, bytes)
        assert len(raw) > 0
        assert len(mapping) >= 40
        assert mapping["1"] == "Tiffany Organ-Bowles"

    def test_court_id(self) -> None:
        assert KernCourtDirectory.COURT_ID == "ca_kern"

    @respx.mock
    def test_fetch_and_snapshot_defaults_court_id(self) -> None:
        html = _load("kern_judicial_officers.html")
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None

        directory = KernCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=mock_conn,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) >= 40
        assert mapping["1"] == "Tiffany Organ-Bowles"
