"""Tests for Contra Costa department-to-judge mapping.

Fixtures:
    cc_index_page_mini.html — minimal CC tentative rulings index page with tr-dir blocks

Source: https://retired.cc-courts.org/civil/motions-hearings-tentative.aspx
The department-to-judge mapping is extracted from the span header of each
``<div class='tr-dir'>`` block: "Department NN - Judge Name".
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.cc_dept_judges import (
    INDEX_URL,
    ContraCostaCourtDirectory,
    DepartmentJudge,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    parse_index_page_html,
)
from courts.ca.la_dept_judges import normalize_department

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_index_page_html — fixture-based tests
# ---------------------------------------------------------------------------


class TestParseIndexPageHtml:
    def test_parses_entries_from_mini_fixture(self) -> None:
        html = _load("cc_index_page_mini.html")
        entries = parse_index_page_html(html)
        # Mini fixture has 12 tr-dir blocks
        assert len(entries) >= 3

    def test_department_14_athanasiou(self) -> None:
        html = _load("cc_index_page_mini.html")
        entries = parse_index_page_html(html)
        dept_14 = [e for e in entries if normalize_department(e.department) == "14"]
        assert len(dept_14) == 1
        assert dept_14[0].judge_name == "Athanasiou"

    def test_department_16_reyes(self) -> None:
        html = _load("cc_index_page_mini.html")
        entries = parse_index_page_html(html)
        dept_16 = [e for e in entries if normalize_department(e.department) == "16"]
        assert len(dept_16) == 1
        assert dept_16[0].judge_name == "Reyes"

    def test_department_30_george_probate_stripped(self) -> None:
        """Probate departments have '(Probate)' suffix in header — should be stripped."""
        html = _load("cc_index_page_mini.html")
        entries = parse_index_page_html(html)
        dept_30 = [e for e in entries if normalize_department(e.department) == "30"]
        assert len(dept_30) == 1
        assert "(Probate)" not in dept_30[0].judge_name
        assert dept_30[0].judge_name == "George"

    def test_leading_zero_normalization(self) -> None:
        """Department 09 should be normalized to '9' in the map."""
        html = _load("cc_index_page_mini.html")
        entries = parse_index_page_html(html)
        # The raw department from the header may be zero-padded
        dept_09 = [e for e in entries if normalize_department(e.department) == "9"]
        assert len(dept_09) == 1
        assert dept_09[0].judge_name == "Devine"

    def test_multi_word_judge_name(self) -> None:
        """Department 34 has 'Judge  Leonard Marquez' (double space) — normalize whitespace."""
        html = _load("cc_index_page_mini.html")
        entries = parse_index_page_html(html)
        dept_34 = [e for e in entries if normalize_department(e.department) == "34"]
        assert len(dept_34) == 1
        # Should not have double spaces
        assert "  " not in dept_34[0].judge_name
        # Multi-word name
        assert "Leonard Marquez" in dept_34[0].judge_name

    def test_commissioner_entry(self) -> None:
        """Department 57 has 'Comm Yamamoto' (no 'Judge' prefix) — still parsed."""
        html = _load("cc_index_page_mini.html")
        entries = parse_index_page_html(html)
        dept_57 = [e for e in entries if normalize_department(e.department) == "57"]
        assert len(dept_57) == 1
        assert dept_57[0].judge_name == "Yamamoto"

    def test_empty_html_returns_empty(self) -> None:
        entries = parse_index_page_html("<html><body></body></html>")
        assert entries == []

    def test_no_tr_dir_blocks_returns_empty(self) -> None:
        html = "<html><body><div class='other'>Nothing here</div></body></html>"
        entries = parse_index_page_html(html)
        assert entries == []


# ---------------------------------------------------------------------------
# URL fallback path — when header span text is absent
# ---------------------------------------------------------------------------


class TestUrlFallback:
    def test_url_fallback_when_no_header_text(self) -> None:
        """If span header has no text, fall back to URL path regex."""
        html = """<html><body>
        <div class="tr-dir">
            <span style="font-weight:bold;"></span>
            <a class="tentative-ruling"
               href="TR\\Department 16 - Judge Reyes\\16_031126.pdf">Mar 11, 2026</a>
        </div>
        </body></html>"""
        entries = parse_index_page_html(html)
        # Should get dept 16 from URL path fallback
        assert len(entries) == 1
        assert entries[0].department == "16"
        assert entries[0].judge_name == "Reyes"


# ---------------------------------------------------------------------------
# build_department_judge_map
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_mini_fixture(self) -> None:
        html = _load("cc_index_page_mini.html")
        entries = parse_index_page_html(html)
        dept_map = build_department_judge_map(entries)
        assert len(dept_map) >= 3

    def test_strips_leading_zeros_in_keys(self) -> None:
        html = _load("cc_index_page_mini.html")
        entries = parse_index_page_html(html)
        dept_map = build_department_judge_map(entries)
        # dept 09 should be normalized to "9"
        assert "9" in dept_map
        assert "09" not in dept_map

    def test_known_departments_present(self) -> None:
        html = _load("cc_index_page_mini.html")
        entries = parse_index_page_html(html)
        dept_map = build_department_judge_map(entries)
        assert dept_map.get("14") == "Athanasiou"
        assert dept_map.get("16") == "Reyes"
        assert dept_map.get("30") == "George"

    def test_duplicate_keeps_first(self) -> None:
        entries = [
            DepartmentJudge(department="16", judge_name="Reyes"),
            DepartmentJudge(department="16", judge_name="Other"),
        ]
        dept_map = build_department_judge_map(entries)
        assert dept_map["16"] == "Reyes"


# ---------------------------------------------------------------------------
# lookup_judge_for_department
# ---------------------------------------------------------------------------


class TestLookupJudgeForDepartment:
    def test_found(self) -> None:
        dept_map = {"14": "Athanasiou", "16": "Reyes"}
        assert lookup_judge_for_department(dept_map, "14") == "Athanasiou"

    def test_found_with_leading_zero(self) -> None:
        """Lookup with zero-padded department normalizes before lookup."""
        dept_map = {"9": "Devine"}
        assert lookup_judge_for_department(dept_map, "09") == "Devine"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"14": "Athanasiou"}
        assert lookup_judge_for_department(dept_map, "99") is None


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_mapping_with_mini_fixture() -> None:
    """fetch_department_judge_mapping returns correct map from fixture."""
    html = _load("cc_index_page_mini.html")
    respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))

    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) >= 3
    assert dept_map.get("14") == "Athanasiou"
    assert dept_map.get("16") == "Reyes"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(INDEX_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# ContraCostaCourtDirectory — class-level tests
# ---------------------------------------------------------------------------


class TestContraCostaCourtDirectory:
    def test_court_id(self) -> None:
        assert ContraCostaCourtDirectory.COURT_ID == "ca_contra_costa"

    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        from unittest.mock import MagicMock

        html = _load("cc_index_page_mini.html")
        respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))

        directory = ContraCostaCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=MagicMock(),
        )
        raw, mapping = directory.fetch_current()

        assert isinstance(raw, bytes)
        assert len(raw) > 0
        assert len(mapping) >= 3
        assert mapping.get("14") == "Athanasiou"
        assert mapping.get("16") == "Reyes"

    @respx.mock
    def test_fetch_and_snapshot_defaults_court_id(self) -> None:
        from unittest.mock import MagicMock

        html = _load("cc_index_page_mini.html")
        respx.get(INDEX_URL).mock(return_value=httpx.Response(200, text=html))

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # No existing snapshot (not a duplicate)
        mock_cursor.fetchone.return_value = None

        directory = ContraCostaCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=mock_conn,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) >= 3
        assert mapping.get("14") == "Athanasiou"


# ---------------------------------------------------------------------------
# Minimum coverage assertion: ≥3 known active departments
# ---------------------------------------------------------------------------


def test_at_least_three_active_departments_from_fixture() -> None:
    """AC1: unit tests must assert non-empty mapping for at least 3 known CC depts."""
    html = _load("cc_index_page_mini.html")
    entries = parse_index_page_html(html)
    dept_map = build_department_judge_map(entries)

    known_active = ["9", "14", "16", "30"]
    found = [d for d in known_active if d in dept_map]
    assert len(found) >= 3, f"Expected >=3 known CC departments, got {len(found)}: {found}"
