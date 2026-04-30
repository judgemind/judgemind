"""Tests for San Diego County department-to-judge mapping — built against real fixtures.

Fixtures:
    sd_judge_assignments.html — San Diego judge assignments page
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from courts.ca.la_dept_judges import normalize_department
from courts.ca.sd_dept_judges import (
    JUDGE_ASSIGNMENTS_URL,
    DepartmentJudge,
    SanDiegoCourtDirectory,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    parse_judge_assignments_html,
)

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_judge_assignments_html — fixture tests
# ---------------------------------------------------------------------------


class TestParseJudgeAssignments:
    def test_parses_20_plus_entries(self) -> None:
        html = _load("sd_judge_assignments.html")
        entries = parse_judge_assignments_html(html)
        assert len(entries) >= 20

    def test_n18_entry(self) -> None:
        html = _load("sd_judge_assignments.html")
        entries = parse_judge_assignments_html(html)
        n18 = [e for e in entries if normalize_department(e.department) == "N-18"]
        assert len(n18) == 1
        assert n18[0].judge_name == "Renee N.G. Stackhouse"

    def test_n27_entry(self) -> None:
        html = _load("sd_judge_assignments.html")
        entries = parse_judge_assignments_html(html)
        n27 = [e for e in entries if normalize_department(e.department) == "N-27"]
        assert len(n27) == 1
        assert n27[0].judge_name == "Cynthia A. Freeland"

    def test_n28_entry(self) -> None:
        html = _load("sd_judge_assignments.html")
        entries = parse_judge_assignments_html(html)
        n28 = [e for e in entries if normalize_department(e.department) == "N-28"]
        assert len(n28) == 1
        assert n28[0].judge_name == "Earl H. Maas"

    def test_correctly_splits_combined_format(self) -> None:
        """Verify the regex correctly splits 'Department {code}Hon. {name}' format."""
        html = _load("sd_judge_assignments.html")
        entries = parse_judge_assignments_html(html)
        # All entries should have non-empty department and judge_name
        for entry in entries:
            assert entry.department, f"Empty department for entry: {entry}"
            assert entry.judge_name, f"Empty judge name for entry: {entry}"
            # Judge name should NOT contain 'Department' or 'Hon.'
            assert "Department" not in entry.judge_name
            assert entry.judge_name[:4] != "Hon."

    def test_department_codes_are_alphanumeric(self) -> None:
        """Department codes should be like N-18, C-65, not full text."""
        html = _load("sd_judge_assignments.html")
        entries = parse_judge_assignments_html(html)
        for entry in entries:
            # Dept codes should be short (< 10 chars)
            assert len(entry.department) < 10, (
                f"Unexpected long department code: {entry.department}"
            )


# ---------------------------------------------------------------------------
# parse_judge_assignments_html — edge cases
# ---------------------------------------------------------------------------


class TestParseEdgeCases:
    def test_empty_html_returns_empty(self) -> None:
        entries = parse_judge_assignments_html("<html><body></body></html>")
        assert entries == []

    def test_no_table_returns_empty(self) -> None:
        entries = parse_judge_assignments_html("<html><body><p>No table</p></body></html>")
        assert entries == []

    def test_table_with_no_matching_rows(self) -> None:
        html = """<html><body>
        <table>
        <tr><th>Header</th></tr>
        <tr><td>Some unrelated content</td></tr>
        </table></body></html>"""
        entries = parse_judge_assignments_html(html)
        assert entries == []

    def test_synthetic_combined_text(self) -> None:
        """Parse a synthetic row with the combined dept+judge format."""
        html = """<html><body>
        <table>
        <tr><th>Department Policies</th><th>Link</th></tr>
        <tr><td>Department X-99Hon. Test Judge Name</td><td>View PDF</td></tr>
        </table></body></html>"""
        entries = parse_judge_assignments_html(html)
        assert len(entries) == 1
        assert entries[0].department == "X-99"
        assert entries[0].judge_name == "Test Judge Name"


# ---------------------------------------------------------------------------
# build_department_judge_map — unit tests
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_fixture(self) -> None:
        html = _load("sd_judge_assignments.html")
        entries = parse_judge_assignments_html(html)
        dept_map = build_department_judge_map(entries)
        assert len(dept_map) >= 20

    def test_department_codes_normalized(self) -> None:
        html = _load("sd_judge_assignments.html")
        entries = parse_judge_assignments_html(html)
        dept_map = build_department_judge_map(entries)
        assert "N-18" in dept_map
        assert dept_map["N-18"] == "Renee N.G. Stackhouse"

    def test_duplicate_keeps_first(self) -> None:
        entries = [
            DepartmentJudge(department="N-18", judge_name="First Judge"),
            DepartmentJudge(department="N-18", judge_name="Second Judge"),
        ]
        dept_map = build_department_judge_map(entries)
        assert dept_map["N-18"] == "First Judge"

    def test_central_dept_gets_both_keys(self) -> None:
        """Purely-numeric Central dept creates both 'NN' and 'C-NN' keys."""
        entries = [DepartmentJudge(department="64", judge_name="Loren G. Freestone")]
        dept_map = build_department_judge_map(entries)
        assert dept_map["64"] == "Loren G. Freestone"
        assert dept_map["C-64"] == "Loren G. Freestone"

    def test_non_numeric_dept_not_aliased(self) -> None:
        """A non-numeric dept like 'N-18' must NOT get a spurious 'C-N-18' alias."""
        entries = [DepartmentJudge(department="N-18", judge_name="Renee N.G. Stackhouse")]
        dept_map = build_department_judge_map(entries)
        assert "N-18" in dept_map
        assert "C-N-18" not in dept_map

    def test_existing_c_nn_entry_not_overwritten(self) -> None:
        """If 'C-64' already exists, the alias write must not overwrite it."""
        entries = [
            DepartmentJudge(department="C-64", judge_name="Original Judge"),
            DepartmentJudge(department="64", judge_name="Loren G. Freestone"),
        ]
        dept_map = build_department_judge_map(entries)
        assert dept_map["C-64"] == "Original Judge"
        assert dept_map["64"] == "Loren G. Freestone"

    def test_fixture_central_depts_have_both_forms(self) -> None:
        """Real fixture: bare-numeric Central entries are aliased to 'C-NN' form."""
        html = _load("sd_judge_assignments.html")
        entries = parse_judge_assignments_html(html)
        dept_map = build_department_judge_map(entries)
        # Dept 64 (Loren G. Freestone) is a bare-numeric Central entry in the fixture.
        assert "64" in dept_map
        assert "C-64" in dept_map
        assert dept_map["64"] == dept_map["C-64"]


# ---------------------------------------------------------------------------
# lookup_judge_for_department — unit tests
# ---------------------------------------------------------------------------


class TestLookupJudgeForDepartment:
    def test_found(self) -> None:
        dept_map = {"N-18": "Renee N.G. Stackhouse"}
        assert lookup_judge_for_department(dept_map, "N-18") == "Renee N.G. Stackhouse"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"N-18": "Renee N.G. Stackhouse"}
        assert lookup_judge_for_department(dept_map, "Z-99") is None


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_mapping_with_fixture() -> None:
    """fetch_department_judge_mapping returns a correct map from fixture HTML."""
    html = _load("sd_judge_assignments.html")
    respx.get(JUDGE_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=html))
    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) >= 20
    assert dept_map["N-18"] == "Renee N.G. Stackhouse"
    assert dept_map["N-27"] == "Cynthia A. Freeland"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(JUDGE_ASSIGNMENTS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# SanDiegoCourtDirectory — tests
# ---------------------------------------------------------------------------


class TestSanDiegoCourtDirectory:
    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        html = _load("sd_judge_assignments.html")
        respx.get(JUDGE_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=html))

        directory = SanDiegoCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=MagicMock(),
        )
        raw, mapping = directory.fetch_current()

        assert isinstance(raw, bytes)
        assert len(raw) > 0
        assert len(mapping) >= 20
        assert mapping["N-18"] == "Renee N.G. Stackhouse"

    def test_court_id(self) -> None:
        assert SanDiegoCourtDirectory.COURT_ID == "ca_san_diego"

    @respx.mock
    def test_fetch_and_snapshot_defaults_court_id(self) -> None:
        html = _load("sd_judge_assignments.html")
        respx.get(JUDGE_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=html))

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None

        directory = SanDiegoCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=mock_conn,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) >= 20
        assert mapping["N-18"] == "Renee N.G. Stackhouse"
