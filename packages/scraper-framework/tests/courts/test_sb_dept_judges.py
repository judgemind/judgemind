"""Tests for San Bernardino department-to-judge mapping -- built against real PDF fixtures.

Fixtures:
    sb_schedule_assignments.pdf  -- SB schedule of assignments PDF (170 pages)

Parsing the 170-page fixture takes ~14 s on an idle laptop. The file used
to parse it once per test (17 times), and xdist hands a file's tests to one
worker back to back, so the whole suite waited on a ~4-minute serial tail.
With two worktrees pushing at once the tail stretched past 20 minutes and
looked like a hang (#4812). The fixture is now parsed once per worker
process; the parse itself is still exercised end to end by the first test
that asks for it.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from courts.ca import sb_dept_judges
from courts.ca.la_dept_judges import normalize_department
from courts.ca.sb_dept_judges import (
    SB_SCHEDULE_URL,
    DepartmentJudge,
    SanBernardinoCourtDirectory,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    parse_schedule_pdf,
)

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_pdf(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


_SCHEDULE_PDF = "sb_schedule_assignments.pdf"


@functools.cache
def _parsed_schedule() -> tuple[DepartmentJudge, ...]:
    """Parse the schedule fixture once per worker process."""
    return tuple(parse_schedule_pdf(_load_pdf(_SCHEDULE_PDF)))


def _fixture_entries() -> list[DepartmentJudge]:
    """Fresh copies of the parsed fixture entries, safe for a test to mutate."""
    return [DepartmentJudge(e.department, e.judge_name) for e in _parsed_schedule()]


@pytest.fixture
def cached_schedule_parse(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Serve the parsed fixture to the fetch paths instead of re-parsing it.

    The HTTP fetch, the parse call site and the map building still run; only
    the repeated 14 s parse of the same bytes is skipped. Any other bytes go
    to the real parser.
    """
    real_parse = sb_dept_judges.parse_schedule_pdf
    fixture_bytes = _load_pdf(_SCHEDULE_PDF)

    def _parse(pdf_bytes: bytes) -> list[DepartmentJudge]:
        if pdf_bytes == fixture_bytes:
            return _fixture_entries()
        return real_parse(pdf_bytes)

    monkeypatch.setattr(sb_dept_judges, "parse_schedule_pdf", _parse)
    yield


# ---------------------------------------------------------------------------
# parse_schedule_pdf -- fixture tests
# ---------------------------------------------------------------------------


class TestParseSchedulePdf:
    """Tests for parsing the SB schedule of assignments PDF fixture."""

    def test_returns_30_plus_entries(self) -> None:
        """The fixture should yield 30+ department assignments."""
        entries = _fixture_entries()
        assert len(entries) >= 30

    def test_barstow_b1(self) -> None:
        """Dept B1 should map to James R. Baxter."""
        entries = _fixture_entries()
        b1 = [e for e in entries if normalize_department(e.department) == "B1"]
        assert len(b1) == 1
        assert b1[0].judge_name == "James R. Baxter"

    def test_rancho_cucamonga_r1(self) -> None:
        """Dept R1 should map to James J. Hosking."""
        entries = _fixture_entries()
        r1 = [e for e in entries if normalize_department(e.department) == "R1"]
        assert len(r1) == 1
        assert r1[0].judge_name == "James J. Hosking"

    def test_sb_justice_center_s1(self) -> None:
        """Dept S1 should map to Joel S. Agron."""
        entries = _fixture_entries()
        s1 = [e for e in entries if normalize_department(e.department) == "S1"]
        assert len(s1) == 1
        assert s1[0].judge_name == "Joel S. Agron"

    def test_fontana_f1(self) -> None:
        """Dept F1 should map to Damian G. Garcia."""
        entries = _fixture_entries()
        f1 = [e for e in entries if normalize_department(e.department) == "F1"]
        assert len(f1) == 1
        assert f1[0].judge_name == "Damian G. Garcia"

    def test_victorville_v1(self) -> None:
        """Dept V1 should map to Jessica Morgan."""
        entries = _fixture_entries()
        v1 = [e for e in entries if normalize_department(e.department) == "V1"]
        assert len(v1) == 1
        assert v1[0].judge_name == "Jessica Morgan"

    def test_juvenile_j1(self) -> None:
        """Dept J1 should map to Todd Riley."""
        entries = _fixture_entries()
        j1 = [e for e in entries if normalize_department(e.department) == "J1"]
        assert len(j1) == 1
        assert j1[0].judge_name == "Todd Riley"

    def test_needles_n1(self) -> None:
        """Dept N1 should map to Kristine L. Eisler."""
        entries = _fixture_entries()
        n1 = [e for e in entries if normalize_department(e.department) == "N1"]
        assert len(n1) == 1
        assert n1[0].judge_name == "Kristine L. Eisler"

    def test_skips_vacant(self) -> None:
        """Vacant entries should be excluded."""
        entries = _fixture_entries()
        names = [e.judge_name.lower() for e in entries]
        assert "vacant" not in names

    def test_name_format_title_case(self) -> None:
        """Names should be in title case."""
        entries = _fixture_entries()
        for entry in entries:
            # Should not be all uppercase
            assert entry.judge_name != entry.judge_name.upper(), (
                f"Name should be title case but got: {entry.judge_name}"
            )

    def test_multiple_districts_represented(self) -> None:
        """Entries should come from multiple districts."""
        entries = _fixture_entries()
        dept_prefixes = {e.department[0] for e in entries if e.department}
        # Should have at least B, F, R, S, V
        assert len(dept_prefixes) >= 5

    def test_sb_family_law_s43(self) -> None:
        """Dept S43 (SB Family Law division) should map to Michael A. Camber."""
        entries = _fixture_entries()
        s43 = [e for e in entries if normalize_department(e.department) == "S43"]
        assert len(s43) == 1
        assert s43[0].judge_name == "Michael A. Camber"

    def test_joshua_tree_m1(self) -> None:
        """Dept M1 (Joshua Tree) should map to Sarah E. Oliver."""
        entries = _fixture_entries()
        m1 = [e for e in entries if normalize_department(e.department) == "M1"]
        assert len(m1) == 1
        assert m1[0].judge_name == "Sarah E. Oliver"


# ---------------------------------------------------------------------------
# build_department_judge_map -- unit tests
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_fixture(self) -> None:
        entries = _fixture_entries()
        dept_map = build_department_judge_map(entries)
        assert len(dept_map) >= 30

    def test_normalizes_departments(self) -> None:
        entries = _fixture_entries()
        dept_map = build_department_judge_map(entries)
        assert "R1" in dept_map
        assert dept_map["R1"] == "James J. Hosking"

    def test_duplicate_keeps_first(self) -> None:
        entries = [
            DepartmentJudge(department="B1", judge_name="First Judge"),
            DepartmentJudge(department="B1", judge_name="Second Judge"),
        ]
        dept_map = build_department_judge_map(entries)
        assert dept_map["B1"] == "First Judge"


# ---------------------------------------------------------------------------
# lookup_judge_for_department -- unit tests
# ---------------------------------------------------------------------------


class TestLookupJudgeForDepartment:
    def test_found(self) -> None:
        dept_map = {"R1": "James J. Hosking", "S1": "Joel S. Agron"}
        assert lookup_judge_for_department(dept_map, "R1") == "James J. Hosking"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"R1": "James J. Hosking"}
        assert lookup_judge_for_department(dept_map, "X99") is None


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping -- mocked HTTP
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("cached_schedule_parse")
@respx.mock
def test_fetch_mapping_with_fixture() -> None:
    """fetch_department_judge_mapping returns a correct map from the fixture."""
    pdf_bytes = _load_pdf("sb_schedule_assignments.pdf")
    respx.get(SB_SCHEDULE_URL).mock(return_value=httpx.Response(200, content=pdf_bytes))

    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) >= 30
    assert dept_map["R1"] == "James J. Hosking"
    assert dept_map["S1"] == "Joel S. Agron"
    assert dept_map["B1"] == "James R. Baxter"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(SB_SCHEDULE_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# SanBernardinoCourtDirectory -- fetch_current
# ---------------------------------------------------------------------------


class TestSanBernardinoCourtDirectory:
    @pytest.mark.usefixtures("cached_schedule_parse")
    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        """fetch_current returns raw PDF bytes and a valid mapping."""
        pdf_bytes = _load_pdf("sb_schedule_assignments.pdf")
        respx.get(SB_SCHEDULE_URL).mock(return_value=httpx.Response(200, content=pdf_bytes))

        directory = SanBernardinoCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=MagicMock(),
        )

        raw, mapping = directory.fetch_current()

        assert isinstance(raw, bytes)
        assert len(raw) > 0
        assert len(mapping) >= 30
        assert mapping["R1"] == "James J. Hosking"
        assert mapping["S1"] == "Joel S. Agron"

    def test_court_id(self) -> None:
        """SanBernardinoCourtDirectory has the correct COURT_ID."""
        assert SanBernardinoCourtDirectory.COURT_ID == "ca_san_bernardino"

    @pytest.mark.usefixtures("cached_schedule_parse")
    @respx.mock
    def test_fetch_and_snapshot_defaults_court_id(self) -> None:
        """fetch_and_snapshot uses COURT_ID by default."""
        pdf_bytes = _load_pdf("sb_schedule_assignments.pdf")
        respx.get(SB_SCHEDULE_URL).mock(return_value=httpx.Response(200, content=pdf_bytes))

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None

        directory = SanBernardinoCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=mock_conn,
        )

        mapping = directory.fetch_and_snapshot()

        assert len(mapping) >= 30
        assert mapping["R1"] == "James J. Hosking"

    @respx.mock
    def test_save_snapshot_uses_pdf_content_type(self) -> None:
        """save_snapshot uploads with application/pdf content type."""
        pdf_bytes = _load_pdf("sb_schedule_assignments.pdf")

        mock_s3 = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None

        directory = SanBernardinoCourtDirectory(
            s3_client=mock_s3,
            s3_bucket="test-bucket",
            db_conn=mock_conn,
        )

        directory.save_snapshot(pdf_bytes, {"R1": "Test Judge"}, "ca_san_bernardino")

        put_call = mock_s3.put_object.call_args
        assert put_call.kwargs["ContentType"] == "application/pdf"
        assert put_call.kwargs["Key"].endswith(".pdf")
