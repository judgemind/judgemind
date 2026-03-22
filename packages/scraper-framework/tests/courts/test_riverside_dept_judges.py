"""Tests for Riverside department-to-judge mapping — built against the riv_page.html fixture.

Fixtures:
    riv_page.html — representative HTML page matching the structure of
    https://www.riverside.courts.ca.gov/online-services/tentative-rulings
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.la_dept_judges import normalize_department
from courts.ca.riverside_dept_judges import (
    INDEX_URL,
    DepartmentJudge,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    parse_index_page_html,
)
from courts.ca.riverside_tentatives import RiversideTentativeRulingsScraper, default_config
from framework import ContentFormat
from framework.models import CapturedDocument

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_index_page_html — against fixture
# ---------------------------------------------------------------------------


class TestParseIndexPageHtml:
    def test_parses_entries_from_fixture(self) -> None:
        html = _load("riv_page.html")
        entries = parse_index_page_html(html)
        assert len(entries) >= 16

    def test_first_entry_fields(self) -> None:
        html = _load("riv_page.html")
        entries = parse_index_page_html(html)
        # PS1 is the first department on the page
        ps1_entries = [e for e in entries if normalize_department(e.department) == "PS1"]
        assert len(ps1_entries) == 1
        assert ps1_entries[0].judge_name == "Arthur Hester III"

    def test_numbered_department(self) -> None:
        html = _load("riv_page.html")
        entries = parse_index_page_html(html)
        dept_01 = [e for e in entries if normalize_department(e.department) == "1"]
        assert len(dept_01) == 1
        assert dept_01[0].judge_name == "Harold Hopp"

    def test_mv_department(self) -> None:
        html = _load("riv_page.html")
        entries = parse_index_page_html(html)
        mv1 = [e for e in entries if normalize_department(e.department) == "MV1"]
        assert len(mv1) == 1
        assert mv1[0].judge_name == "David E. Gregory"

    def test_corona_department(self) -> None:
        html = _load("riv_page.html")
        entries = parse_index_page_html(html)
        c1 = [e for e in entries if normalize_department(e.department) == "C1"]
        assert len(c1) == 1
        assert c1[0].judge_name == "Tamara Wagner"

    def test_empty_html_returns_empty(self) -> None:
        entries = parse_index_page_html("<html><body></body></html>")
        assert entries == []

    def test_no_pdf_links_returns_empty(self) -> None:
        html = '<html><body><a href="/page">Not a PDF</a></body></html>'
        entries = parse_index_page_html(html)
        assert entries == []

    def test_deduplicates_departments(self) -> None:
        """If a department appears multiple times, only the first is kept."""
        html = """<html><body>
        <a href="/file1.pdf">Department PS1 - Honorable Arthur Hester III</a>
        <a href="/file2.pdf">Department PS1 - Honorable Other Judge</a>
        </body></html>"""
        entries = parse_index_page_html(html)
        assert len(entries) == 1
        assert entries[0].judge_name == "Arthur Hester III"


# ---------------------------------------------------------------------------
# build_department_judge_map — unit tests
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_fixture(self) -> None:
        html = _load("riv_page.html")
        entries = parse_index_page_html(html)
        dept_map = build_department_judge_map(entries)
        assert len(dept_map) >= 16

    def test_normalizes_padded_departments(self) -> None:
        """Departments like '01' should be accessible as '1'."""
        html = _load("riv_page.html")
        entries = parse_index_page_html(html)
        dept_map = build_department_judge_map(entries)
        assert "1" in dept_map
        assert dept_map["1"] == "Harold Hopp"

    def test_alphanumeric_dept_preserved(self) -> None:
        html = _load("riv_page.html")
        entries = parse_index_page_html(html)
        dept_map = build_department_judge_map(entries)
        assert "PS1" in dept_map
        assert dept_map["PS1"] == "Arthur Hester III"

    def test_duplicate_keeps_first(self) -> None:
        entries = [
            DepartmentJudge(department="10", judge_name="First Judge"),
            DepartmentJudge(department="10", judge_name="Second Judge"),
        ]
        dept_map = build_department_judge_map(entries)
        assert dept_map["10"] == "First Judge"


# ---------------------------------------------------------------------------
# lookup_judge_for_department — unit tests
# ---------------------------------------------------------------------------


class TestLookupJudgeForDepartment:
    def test_found(self) -> None:
        dept_map = {"PS1": "Arthur Hester III", "1": "Harold Hopp"}
        assert lookup_judge_for_department(dept_map, "PS1") == "Arthur Hester III"

    def test_found_with_padded_input(self) -> None:
        dept_map = {"1": "Harold Hopp"}
        assert lookup_judge_for_department(dept_map, "01") == "Harold Hopp"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"PS1": "Arthur Hester III"}
        assert lookup_judge_for_department(dept_map, "99") is None

    def test_alphanumeric_dept(self) -> None:
        dept_map = {"MV1": "David E. Gregory"}
        assert lookup_judge_for_department(dept_map, "MV1") == "David E. Gregory"


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_mapping_with_fixture() -> None:
    """fetch_department_judge_mapping returns a correct map from fixture HTML."""
    html = _load("riv_page.html")
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))
    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) >= 16
    assert dept_map["PS1"] == "Arthur Hester III"
    assert dept_map["1"] == "Harold Hopp"
    assert dept_map["MV1"] == "David E. Gregory"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(INDEX_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# Integration: Riverside scraper should use mapping to populate judge_name
# ---------------------------------------------------------------------------


class TestScraperDeptJudgeMapFallback:
    """Test that RiversideTentativeRulingsScraper uses the dept-judge
    mapping as a fallback when the ruling text doesn't contain a judge name."""

    def _make_doc_without_judge(self, department: str = "PS1") -> CapturedDocument:
        """Create a ruling doc that has no judge name."""
        from datetime import datetime

        return CapturedDocument(
            scraper_id="ca-riverside-tentatives-civil",
            state="CA",
            county="Riverside",
            court="Superior Court",
            source_url="https://example.com/ruling.pdf",
            capture_timestamp=datetime(2026, 3, 2, 18, 0, 0),
            content_format=ContentFormat.PDF,
            raw_content=b"dummy pdf content",
            content_hash="",
            department=department,
        )

    def test_populates_judge_from_mapping(self) -> None:
        """When ruling has no judge name, use dept mapping."""
        dept_map = {"PS1": "Arthur Hester III", "1": "Harold Hopp"}
        config = default_config()
        scraper = RiversideTentativeRulingsScraper(config=config, dept_judge_map=dept_map)

        doc = self._make_doc_without_judge(department="PS1")
        # Mark as pre_split to skip PDF parsing (which needs real PDF bytes)
        doc.extra["pre_split"] = True
        doc.ruling_text = "Some ruling text without a judge name"
        result = scraper.parse_document(doc)
        assert result.judge_name == "Arthur Hester III"

    def test_does_not_override_existing_judge(self) -> None:
        """When ruling already has a judge name, don't override with mapping."""
        dept_map = {"PS1": "Arthur Hester III"}
        config = default_config()
        scraper = RiversideTentativeRulingsScraper(config=config, dept_judge_map=dept_map)

        doc = self._make_doc_without_judge(department="PS1")
        doc.extra["pre_split"] = True
        doc.ruling_text = "Some ruling text"
        doc.judge_name = "Existing Judge Name"
        result = scraper.parse_document(doc)
        assert result.judge_name == "Existing Judge Name"

    def test_no_mapping_leaves_judge_none(self) -> None:
        """Without a dept_judge_map, judge_name stays None."""
        config = default_config()
        scraper = RiversideTentativeRulingsScraper(config=config)

        doc = self._make_doc_without_judge(department="PS1")
        doc.extra["pre_split"] = True
        doc.ruling_text = "Some ruling text"
        result = scraper.parse_document(doc)
        assert result.judge_name is None

    def test_unknown_dept_leaves_judge_none(self) -> None:
        """If department not in mapping, judge_name stays None."""
        dept_map = {"PS1": "Arthur Hester III"}
        config = default_config()
        scraper = RiversideTentativeRulingsScraper(config=config, dept_judge_map=dept_map)

        doc = self._make_doc_without_judge(department="PS99")
        doc.extra["pre_split"] = True
        doc.ruling_text = "Some ruling text"
        result = scraper.parse_document(doc)
        assert result.judge_name is None


# ---------------------------------------------------------------------------
# Mapping matches departments seen in investigation #572
# ---------------------------------------------------------------------------


def test_mapping_covers_investigation_departments() -> None:
    """Verify the fixture mapping covers the 15 departments from investigation #572.

    The departments missing judge_id: 2, PS2, 4, C1, M302, PS1, 10, 7, M301,
    MV1, 3, PS4, 1, 5, M205.
    """
    html = _load("riv_page.html")
    entries = parse_index_page_html(html)
    dept_map = build_department_judge_map(entries)

    expected_depts = [
        "2",
        "PS2",
        "4",
        "C1",
        "M302",
        "PS1",
        "10",
        "7",
        "M301",
        "MV1",
        "3",
        "PS4",
        "1",
        "5",
        "M205",
    ]
    for dept in expected_depts:
        norm = normalize_department(dept)
        assert norm in dept_map, f"Department {dept} (normalized: {norm}) not in mapping"
