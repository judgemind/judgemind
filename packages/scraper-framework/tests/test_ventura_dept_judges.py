"""Tests for Ventura department-to-judge mapping — built against real fixtures.

Fixtures:
    ventura_assignments_page.html          — main courthouse judicial assignments
    ventura_juvenile_assignments_page.html  — juvenile courthouse assignments
    ventura_east_county_assignments_page.html — east county courthouse assignments
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.la_dept_judges import normalize_department
from courts.ca.ventura_dept_judges import (
    EAST_COUNTY_ASSIGNMENTS_URL,
    JUVENILE_ASSIGNMENTS_URL,
    VENTURA_ASSIGNMENTS_URL,
    DepartmentJudge,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    parse_assignments_page_html,
)

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_assignments_page_html — main courthouse fixture
# ---------------------------------------------------------------------------


class TestParseMainCourthouse:
    def test_parses_entries_from_fixture(self) -> None:
        html = _load("ventura_assignments_page.html")
        entries = parse_assignments_page_html(html)
        # Main courthouse has many departments (10-48 range, minus vacants)
        assert len(entries) >= 20

    def test_civil_department_42(self) -> None:
        html = _load("ventura_assignments_page.html")
        entries = parse_assignments_page_html(html)
        dept_42 = [e for e in entries if normalize_department(e.department) == "42"]
        assert len(dept_42) == 1
        assert dept_42[0].judge_name == "Ronda J. McKaig"

    def test_civil_department_20(self) -> None:
        html = _load("ventura_assignments_page.html")
        entries = parse_assignments_page_html(html)
        dept_20 = [e for e in entries if normalize_department(e.department) == "20"]
        assert len(dept_20) == 1
        assert dept_20[0].judge_name == "Maureen M. Houska"

    def test_department_41(self) -> None:
        html = _load("ventura_assignments_page.html")
        entries = parse_assignments_page_html(html)
        dept_41 = [e for e in entries if normalize_department(e.department) == "41"]
        assert len(dept_41) == 1
        assert dept_41[0].judge_name == "Kevin G. DeNoce"

    def test_department_43(self) -> None:
        html = _load("ventura_assignments_page.html")
        entries = parse_assignments_page_html(html)
        dept_43 = [e for e in entries if normalize_department(e.department) == "43"]
        assert len(dept_43) == 1
        assert dept_43[0].judge_name == "Benjamin F. Coats"

    def test_skips_vacant_departments(self) -> None:
        html = _load("ventura_assignments_page.html")
        entries = parse_assignments_page_html(html)
        names = [e.judge_name for e in entries]
        assert "Vacant" not in names

    def test_strips_hon_prefix(self) -> None:
        html = _load("ventura_assignments_page.html")
        entries = parse_assignments_page_html(html)
        for entry in entries:
            assert not entry.judge_name.startswith("Hon.")


# ---------------------------------------------------------------------------
# parse_assignments_page_html — juvenile courthouse fixture
# ---------------------------------------------------------------------------


class TestParseJuvenileCourthouse:
    def test_parses_entries_from_fixture(self) -> None:
        html = _load("ventura_juvenile_assignments_page.html")
        entries = parse_assignments_page_html(html)
        # Juvenile courthouse has J1-J6, minus vacants
        assert len(entries) >= 4

    def test_department_j6(self) -> None:
        """J6 is the most common department in Ventura rulings."""
        html = _load("ventura_juvenile_assignments_page.html")
        entries = parse_assignments_page_html(html)
        j6 = [e for e in entries if normalize_department(e.department) == "J6"]
        assert len(j6) == 1
        assert j6[0].judge_name == "Gilbert A. Romero"

    def test_department_j1(self) -> None:
        html = _load("ventura_juvenile_assignments_page.html")
        entries = parse_assignments_page_html(html)
        j1 = [e for e in entries if normalize_department(e.department) == "J1"]
        assert len(j1) == 1
        assert j1[0].judge_name == "Carol L. Hubner"


# ---------------------------------------------------------------------------
# parse_assignments_page_html — east county fixture
# ---------------------------------------------------------------------------


class TestParseEastCountyCourthouse:
    def test_parses_entries_from_fixture(self) -> None:
        html = _load("ventura_east_county_assignments_page.html")
        entries = parse_assignments_page_html(html)
        # East county has S1-S5, many vacant
        assert len(entries) >= 1

    def test_department_s1(self) -> None:
        html = _load("ventura_east_county_assignments_page.html")
        entries = parse_assignments_page_html(html)
        s1 = [e for e in entries if normalize_department(e.department) == "S1"]
        assert len(s1) == 1
        assert s1[0].judge_name == "Courtney M. Lewis"


# ---------------------------------------------------------------------------
# parse_assignments_page_html — edge cases
# ---------------------------------------------------------------------------


class TestParseEdgeCases:
    def test_empty_html_returns_empty(self) -> None:
        entries = parse_assignments_page_html("<html><body></body></html>")
        assert entries == []

    def test_no_tablesorter_returns_empty(self) -> None:
        html = "<html><body><table><tr><td>Not a match</td></tr></table></body></html>"
        entries = parse_assignments_page_html(html)
        assert entries == []

    def test_tablesorter_without_courtroom_header_returns_empty(self) -> None:
        """A tablesorter table without a 'Courtroom' header should be skipped."""
        html = """<html><body>
        <table class="tablesorter">
        <thead><tr><th>Judicial Officer</th><th>Title</th></tr></thead>
        <tbody><tr><td>Hon. John Doe</td><td>Judge</td></tr></tbody>
        </table></body></html>"""
        entries = parse_assignments_page_html(html)
        assert entries == []


# ---------------------------------------------------------------------------
# build_department_judge_map — unit tests
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_main_fixture(self) -> None:
        html = _load("ventura_assignments_page.html")
        entries = parse_assignments_page_html(html)
        dept_map = build_department_judge_map(entries)
        assert len(dept_map) >= 20

    def test_normalizes_departments(self) -> None:
        html = _load("ventura_assignments_page.html")
        entries = parse_assignments_page_html(html)
        dept_map = build_department_judge_map(entries)
        assert "42" in dept_map
        assert dept_map["42"] == "Ronda J. McKaig"

    def test_alphanumeric_dept_preserved(self) -> None:
        html = _load("ventura_juvenile_assignments_page.html")
        entries = parse_assignments_page_html(html)
        dept_map = build_department_judge_map(entries)
        assert "J6" in dept_map
        assert dept_map["J6"] == "Gilbert A. Romero"

    def test_duplicate_keeps_first(self) -> None:
        entries = [
            DepartmentJudge(department="42", judge_name="First Judge", title="Judge"),
            DepartmentJudge(department="42", judge_name="Second Judge", title="Judge"),
        ]
        dept_map = build_department_judge_map(entries)
        assert dept_map["42"] == "First Judge"


# ---------------------------------------------------------------------------
# lookup_judge_for_department — unit tests
# ---------------------------------------------------------------------------


class TestLookupJudgeForDepartment:
    def test_found(self) -> None:
        dept_map = {"42": "Ronda J. McKaig", "J6": "Gilbert A. Romero"}
        assert lookup_judge_for_department(dept_map, "42") == "Ronda J. McKaig"

    def test_found_alphanumeric(self) -> None:
        dept_map = {"J6": "Gilbert A. Romero"}
        assert lookup_judge_for_department(dept_map, "J6") == "Gilbert A. Romero"

    def test_found_with_padded_input(self) -> None:
        dept_map = {"42": "Ronda J. McKaig"}
        assert lookup_judge_for_department(dept_map, "042") == "Ronda J. McKaig"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"42": "Ronda J. McKaig"}
        assert lookup_judge_for_department(dept_map, "99") is None


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_mapping_with_fixtures() -> None:
    """fetch_department_judge_mapping returns a correct map from all three fixtures."""
    main_html = _load("ventura_assignments_page.html")
    juvenile_html = _load("ventura_juvenile_assignments_page.html")
    east_html = _load("ventura_east_county_assignments_page.html")

    respx.get(VENTURA_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=main_html))
    respx.get(JUVENILE_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=juvenile_html))
    respx.get(EAST_COUNTY_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=east_html))

    dept_map = fetch_department_judge_mapping()
    # Should have departments from all three courthouses
    assert len(dept_map) >= 22
    # Main courthouse
    assert dept_map["42"] == "Ronda J. McKaig"
    assert dept_map["20"] == "Maureen M. Houska"
    # Juvenile courthouse
    assert dept_map["J6"] == "Gilbert A. Romero"
    # East county courthouse
    assert dept_map["S1"] == "Courtney M. Lewis"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(VENTURA_ASSIGNMENTS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# Combined mapping covers departments from issue #1103
# ---------------------------------------------------------------------------


def test_mapping_covers_observed_departments() -> None:
    """Verify the fixture mapping covers the departments observed in Ventura rulings.

    Issue #1103 reports rulings in departments J6, 42, and 41 (the most common).
    """
    main_html = _load("ventura_assignments_page.html")
    juvenile_html = _load("ventura_juvenile_assignments_page.html")

    main_entries = parse_assignments_page_html(main_html)
    juvenile_entries = parse_assignments_page_html(juvenile_html)

    all_entries = main_entries + juvenile_entries
    dept_map = build_department_judge_map(all_entries)

    observed_depts = ["J6", "42", "41"]
    for dept in observed_depts:
        norm = normalize_department(dept)
        assert norm in dept_map, f"Department {dept} (normalized: {norm}) not in mapping"
