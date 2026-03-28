"""Tests for SF department-to-judge mapping -- built against real PDF fixtures.

Fixtures:
    sf_dept_listing.pdf  -- SF department listing PDF (2 pages)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from courts.ca.la_dept_judges import normalize_department
from courts.ca.sf_dept_judges import (
    SF_ASSIGNMENTS_URL,
    DepartmentJudge,
    SFCourtDirectory,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    parse_dept_listing_pdf,
)

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_pdf(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# parse_dept_listing_pdf -- fixture tests
# ---------------------------------------------------------------------------


class TestParseDeptListingPdf:
    """Tests for parsing the SF department listing PDF fixture."""

    def test_returns_30_plus_entries(self) -> None:
        """The fixture should yield 30+ department assignments."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        assert len(entries) >= 30

    def test_civic_center_dept_301(self) -> None:
        """Dept 301 should map to Christine Van Aken."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        dept_301 = [e for e in entries if normalize_department(e.department) == "301"]
        assert len(dept_301) == 1
        assert dept_301[0].judge_name == "Christine Van Aken"

    def test_civic_center_dept_204(self) -> None:
        """Dept 204 should map to Ross C. Moody (first occurrence kept)."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        dept_204 = [e for e in entries if normalize_department(e.department) == "204"]
        assert len(dept_204) == 1
        assert dept_204[0].judge_name == "Ross C. Moody"

    def test_civic_center_dept_304(self) -> None:
        """Dept 304 should map to Ethan P. Schulman."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        dept_304 = [e for e in entries if normalize_department(e.department) == "304"]
        assert len(dept_304) == 1
        assert dept_304[0].judge_name == "Ethan P. Schulman"

    def test_hoj_dept_9(self) -> None:
        """Dept 9 (Hall of Justice) should map to Simon J. Frankel."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        dept_9 = [e for e in entries if normalize_department(e.department) == "9"]
        assert len(dept_9) == 1
        assert dept_9[0].judge_name == "Simon J. Frankel"

    def test_hoj_dept_with_floor_suffix(self) -> None:
        """Floor suffixes like '13--2nd flr' should be stripped to '13'."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        dept_13 = [e for e in entries if normalize_department(e.department) == "13"]
        assert len(dept_13) == 1
        assert dept_13[0].judge_name == "Brian L. Ferrall"

    def test_skips_vacant(self) -> None:
        """Vacant entries should be excluded."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        names = [e.judge_name.lower() for e in entries]
        assert "vacant" not in names

    def test_skips_visiting_judge(self) -> None:
        """Visiting Judge entries should be excluded."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        names = [e.judge_name.lower() for e in entries]
        assert "visiting judge" not in names

    def test_skips_unassigned(self) -> None:
        """UNASSIGNED entries should be excluded."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        names = [e.judge_name.lower() for e in entries]
        assert "unassigned" not in names

    def test_name_format_first_last(self) -> None:
        """Names should be in 'First Last' format, not 'LAST, First'."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        for entry in entries:
            assert "," not in entry.judge_name, (
                f"Name should be 'First Last' but got: {entry.judge_name}"
            )

    def test_jjc_dept(self) -> None:
        """Juvenile Justice Center departments should be included."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        # JJC Dept 3 = Darwin, Richard C.
        dept_3 = [e for e in entries if normalize_department(e.department) == "3"]
        assert len(dept_3) == 1
        assert dept_3[0].judge_name == "Richard C. Darwin"

    def test_dept_606(self) -> None:
        """Dept 606 should map to Stephen M. Murphy."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        dept_606 = [e for e in entries if normalize_department(e.department) == "606"]
        assert len(dept_606) == 1
        assert dept_606[0].judge_name == "Stephen M. Murphy"

    def test_mcnaughton_casing(self) -> None:
        """McNaughton should be properly title-cased with capital N."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        dept_21 = [e for e in entries if normalize_department(e.department) == "21"]
        assert len(dept_21) == 1
        assert dept_21[0].judge_name == "Michael McNaughton"


# ---------------------------------------------------------------------------
# build_department_judge_map -- unit tests
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_fixture(self) -> None:
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        dept_map = build_department_judge_map(entries)
        assert len(dept_map) >= 30

    def test_normalizes_departments(self) -> None:
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        entries = parse_dept_listing_pdf(pdf_bytes)
        dept_map = build_department_judge_map(entries)
        assert "301" in dept_map
        assert dept_map["301"] == "Christine Van Aken"

    def test_duplicate_keeps_first(self) -> None:
        entries = [
            DepartmentJudge(department="42", judge_name="First Judge"),
            DepartmentJudge(department="42", judge_name="Second Judge"),
        ]
        dept_map = build_department_judge_map(entries)
        assert dept_map["42"] == "First Judge"


# ---------------------------------------------------------------------------
# lookup_judge_for_department -- unit tests
# ---------------------------------------------------------------------------


class TestLookupJudgeForDepartment:
    def test_found(self) -> None:
        dept_map = {"301": "Christine Van Aken", "9": "Simon J. Frankel"}
        assert lookup_judge_for_department(dept_map, "301") == "Christine Van Aken"

    def test_found_with_padded_input(self) -> None:
        dept_map = {"9": "Simon J. Frankel"}
        assert lookup_judge_for_department(dept_map, "009") == "Simon J. Frankel"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"301": "Christine Van Aken"}
        assert lookup_judge_for_department(dept_map, "999") is None


# ---------------------------------------------------------------------------
# _find_latest_dept_listing_url -- HTML parsing
# ---------------------------------------------------------------------------


class TestFindLatestDeptListingUrl:
    def test_finds_dept_listing_link(self) -> None:
        from courts.ca.sf_dept_judges import _find_latest_dept_listing_url

        html = """<html><body>
        <a href="/system/files/general/sftc-judges-deptassignment-listing-public-102025.pdf">
            Judicial Roster Department Listing
        </a>
        <a href="/system/files/general/sftc-judges-roster-alpha-public-102025.pdf">
            Judicial Roster Alphabetical List
        </a>
        </body></html>"""
        url = _find_latest_dept_listing_url(html)
        assert url is not None
        assert "deptassignment-listing" in url
        assert url.startswith("https://sf.courts.ca.gov/")

    def test_returns_none_when_no_match(self) -> None:
        from courts.ca.sf_dept_judges import _find_latest_dept_listing_url

        html = "<html><body><a href='/other.pdf'>Other</a></body></html>"
        url = _find_latest_dept_listing_url(html)
        assert url is None


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping -- mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_mapping_with_fixture() -> None:
    """fetch_department_judge_mapping returns a correct map from the fixture."""
    pdf_bytes = _load_pdf("sf_dept_listing.pdf")

    # Mock the assignments page HTML
    assignments_html = """<html><body>
    <a href="/system/files/general/sftc-judges-deptassignment-listing-public-102025.pdf">
        Judicial Roster Department Listing
    </a>
    </body></html>"""

    respx.get(SF_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=assignments_html))
    respx.get(
        "https://sf.courts.ca.gov/system/files/general/sftc-judges-deptassignment-listing-public-102025.pdf"
    ).mock(return_value=httpx.Response(200, content=pdf_bytes))

    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) >= 30
    assert dept_map["301"] == "Christine Van Aken"
    assert dept_map["9"] == "Simon J. Frankel"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(SF_ASSIGNMENTS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# SFCourtDirectory -- fetch_current
# ---------------------------------------------------------------------------


class TestSFCourtDirectory:
    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        """fetch_current returns raw PDF bytes and a valid mapping."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        assignments_html = """<html><body>
        <a href="/system/files/general/sftc-judges-deptassignment-listing-public-102025.pdf">
            Judicial Roster Department Listing
        </a>
        </body></html>"""

        respx.get(SF_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=assignments_html))
        respx.get(
            "https://sf.courts.ca.gov/system/files/general/sftc-judges-deptassignment-listing-public-102025.pdf"
        ).mock(return_value=httpx.Response(200, content=pdf_bytes))

        directory = SFCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=MagicMock(),
        )

        raw, mapping = directory.fetch_current()

        assert isinstance(raw, bytes)
        assert len(raw) > 0
        assert len(mapping) >= 30
        assert mapping["301"] == "Christine Van Aken"
        assert mapping["9"] == "Simon J. Frankel"

    def test_court_id(self) -> None:
        """SFCourtDirectory has the correct COURT_ID."""
        assert SFCourtDirectory.COURT_ID == "ca_san_francisco"

    @respx.mock
    def test_fetch_and_snapshot_defaults_court_id(self) -> None:
        """fetch_and_snapshot uses COURT_ID by default."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        assignments_html = """<html><body>
        <a href="/system/files/general/sftc-judges-deptassignment-listing-public-102025.pdf">
            Judicial Roster Department Listing
        </a>
        </body></html>"""

        respx.get(SF_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=assignments_html))
        respx.get(
            "https://sf.courts.ca.gov/system/files/general/sftc-judges-deptassignment-listing-public-102025.pdf"
        ).mock(return_value=httpx.Response(200, content=pdf_bytes))

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None

        directory = SFCourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=mock_conn,
        )

        mapping = directory.fetch_and_snapshot()

        assert len(mapping) >= 30
        assert mapping["301"] == "Christine Van Aken"

    @respx.mock
    def test_save_snapshot_uses_pdf_content_type(self) -> None:
        """save_snapshot uploads with application/pdf content type."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")

        mock_s3 = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = None

        directory = SFCourtDirectory(
            s3_client=mock_s3,
            s3_bucket="test-bucket",
            db_conn=mock_conn,
        )

        directory.save_snapshot(pdf_bytes, {"301": "Test Judge"}, "ca_san_francisco")

        put_call = mock_s3.put_object.call_args
        assert put_call.kwargs["ContentType"] == "application/pdf"
        assert put_call.kwargs["Key"].endswith(".pdf")
