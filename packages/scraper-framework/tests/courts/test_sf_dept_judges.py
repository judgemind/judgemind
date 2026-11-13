"""Tests for SF department-to-judge mapping -- built against real fixtures.

Fixtures:
    sf_dept_listing.pdf               -- legacy SF department listing PDF (2 pages)
    sf_judicial_assignments_2026.html -- trimmed live capture of the 2026 assignments
                                         page (inline HTML tables, no PDF; #4733)
    sf_home_nav.html                  -- trimmed live home page nav linking to it
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from courts.ca.la_dept_judges import normalize_department
from courts.ca.sf_dept_judges import (
    SF_ASSIGNMENTS_URL,
    SF_HOME_URL,
    DepartmentJudge,
    SFCourtDirectory,
    _find_assignments_page_url,
    _find_latest_dept_listing_url,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    parse_assignments_html,
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
# parse_assignments_html -- live HTML fixture (2026-11, #4733)
# ---------------------------------------------------------------------------
#
# In late 2026 the court replaced the department-listing PDF with inline HTML
# tables on a year-suffixed page (/general-information/judicial-assignments-2026).
# The old /general-information/judicial-assignments URL now 301s to /system/403.


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture(scope="module")
def dept_map() -> dict[str, str]:
    entries = parse_assignments_html(_load_html("sf_judicial_assignments_2026.html"))
    return build_department_judge_map(entries)


class TestParseAssignmentsHtml:
    """Tests against the trimmed live capture of the 2026 assignments page."""

    def test_returns_30_plus_departments(self, dept_map: dict[str, str]) -> None:
        assert len(dept_map) >= 30

    def test_civic_center_departments(self, dept_map: dict[str, str]) -> None:
        assert dept_map["206"] == "Rochelle East"
        assert dept_map["301"] == "Christine Van Aken"
        assert dept_map["304"] == "Ethan P. Schulman"
        assert dept_map["220"] == "Joseph M. Quinn"

    def test_duplicate_department_keeps_first(self, dept_map: dict[str, str]) -> None:
        """Dept 204 lists Roeca (supervising) then McNaughton; keep the first."""
        assert dept_map["204"] == "Russ Roeca"

    def test_missing_space_after_initial(self, dept_map: dict[str, str]) -> None:
        """'CHUN, A.Marisa' normalizes to 'A. Marisa Chun'."""
        assert dept_map["302"] == "A. Marisa Chun"

    def test_hall_of_justice_floor_suffix(self, dept_map: dict[str, str]) -> None:
        """'9—1<sup>st</sup> flr' normalizes to department '9'."""
        assert dept_map["9"] == "Michelle Tong"
        assert dept_map["13"] == "Brian L. Ferrall"

    def test_juvenile_justice_center(self, dept_map: dict[str, str]) -> None:
        assert dept_map["2"] == "Roger C. Chan"

    def test_trailing_period_on_first_name_stripped(self, dept_map: dict[str, str]) -> None:
        """'WILEY, Monica.' normalizes to 'Monica Wiley' (initials keep their period)."""
        assert dept_map["4"] == "Monica Wiley"

    def test_community_justice_center_bare_cells(self, dept_map: dict[str, str]) -> None:
        """The CJC table has no <p>/<strong> wrappers on the dept cell."""
        assert dept_map["8"] == "Julia V. Cervantes"

    def test_no_header_or_placeholder_rows(self, dept_map: dict[str, str]) -> None:
        for dept, judge in dept_map.items():
            assert dept.upper() not in ("DEPT", "DEPARTMENT")
            assert "judge" not in judge.lower(), (dept, judge)
            assert "vacant" not in judge.lower(), (dept, judge)

    def test_all_departments_look_like_departments(self, dept_map: dict[str, str]) -> None:
        import re

        for dept in dept_map:
            assert re.fullmatch(r"[A-Za-z]?\d+[A-Za-z]?", dept), dept

    def test_ignores_tables_without_dept_and_judge_headers(self) -> None:
        html = """<table><thead><tr><th>Name</th><th>Phone</th></tr></thead>
        <tbody><tr><td>301</td><td>Someone</td></tr></tbody></table>"""
        assert parse_assignments_html(html) == []

    def test_skips_visiting_and_vacant(self) -> None:
        html = """<table><thead><tr><th>DEPT</th><th>JUDGE</th></tr></thead><tbody>
        <tr><td>501</td><td>Visiting Judge</td></tr>
        <tr><td>502</td><td>VACANT</td></tr>
        <tr><td>503</td><td><strong>DOE, </strong>Jane</td></tr>
        </tbody></table>"""
        entries = parse_assignments_html(html)
        assert [(e.department, e.judge_name) for e in entries] == [("503", "Jane Doe")]

    def test_header_row_in_tbody(self) -> None:
        """Tables without <thead> use the first row as the header."""
        html = """<table><tr><td>Department</td><td>Judicial Officer</td></tr>
        <tr><td>12</td><td>SMITH, John Q.</td></tr></table>"""
        entries = parse_assignments_html(html)
        assert [(e.department, e.judge_name) for e in entries] == [("12", "John Q. Smith")]


# ---------------------------------------------------------------------------
# _find_assignments_page_url -- discovery from the home page nav
# ---------------------------------------------------------------------------


class TestFindAssignmentsPageUrl:
    def test_finds_link_in_live_home_nav(self) -> None:
        url = _find_assignments_page_url(_load_html("sf_home_nav.html"))
        assert url == "https://sf.courts.ca.gov/general-information/judicial-assignments-2026"

    def test_matches_on_link_text_not_href(self) -> None:
        """A future renamed slug is still found by its 'Judicial Assignments' text."""
        html = '<a href="/gi/roster-2027">Judicial Assignments</a>'
        assert _find_assignments_page_url(html) == "https://sf.courts.ca.gov/gi/roster-2027"

    def test_matches_year_suffixed_link_text(self) -> None:
        html = '<a href="/x">Judicial Awards</a><a href="/y">Judicial Assignments 2027</a>'
        assert _find_assignments_page_url(html) == "https://sf.courts.ca.gov/y"

    def test_falls_back_to_href_slug(self) -> None:
        html = '<a href="/general-information/judicial-assignments-2027">Assignments</a>'
        assert (
            _find_assignments_page_url(html)
            == "https://sf.courts.ca.gov/general-information/judicial-assignments-2027"
        )

    def test_returns_none_when_absent(self) -> None:
        html = '<a href="/general-information/judicial-awards-and-recognition">Awards</a>'
        assert _find_assignments_page_url(html) is None


# ---------------------------------------------------------------------------
# _find_latest_dept_listing_url -- legacy PDF link discovery
# ---------------------------------------------------------------------------


class TestFindLatestDeptListingUrl:
    def test_finds_dept_listing_link(self) -> None:
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

    def test_finds_pdf_by_link_text(self) -> None:
        html = '<a href="/system/files/general/roster-2027.pdf">Department Listing (PDF)</a>'
        assert _find_latest_dept_listing_url(html) == (
            "https://sf.courts.ca.gov/system/files/general/roster-2027.pdf"
        )

    def test_returns_none_when_no_match(self) -> None:
        html = "<html><body><a href='/other.pdf'>Other</a></body></html>"
        assert _find_latest_dept_listing_url(html) is None

    def test_live_2026_page_has_no_pdf_link(self) -> None:
        assert (
            _find_latest_dept_listing_url(_load_html("sf_judicial_assignments_2026.html")) is None
        )


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping -- mocked HTTP
# ---------------------------------------------------------------------------

_LIVE_ASSIGNMENTS_URL = "https://sf.courts.ca.gov/general-information/judicial-assignments-2026"
_PDF_URL = "https://sf.courts.ca.gov/system/files/general/sftc-judges-deptassignment-listing-public-102025.pdf"
_PDF_LINK_PAGE = """<html><body>
<a href="/system/files/general/sftc-judges-deptassignment-listing-public-102025.pdf">
    Judicial Roster Department Listing
</a>
</body></html>"""


def _mock_live_site() -> None:
    respx.get(SF_HOME_URL).mock(
        return_value=httpx.Response(200, text=_load_html("sf_home_nav.html"))
    )
    respx.get(_LIVE_ASSIGNMENTS_URL).mock(
        return_value=httpx.Response(200, text=_load_html("sf_judicial_assignments_2026.html"))
    )


@respx.mock
def test_fetch_mapping_from_live_html_fixture() -> None:
    """Home nav -> 2026 assignments page -> inline HTML tables."""
    _mock_live_site()
    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) >= 30
    assert dept_map["301"] == "Christine Van Aken"
    assert dept_map["9"] == "Michelle Tong"


@respx.mock
def test_fetch_mapping_pdf_fallback() -> None:
    """When the assignments page links a dept-listing PDF instead of tables, parse the PDF."""
    respx.get(SF_HOME_URL).mock(
        return_value=httpx.Response(200, text=_load_html("sf_home_nav.html"))
    )
    respx.get(_LIVE_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=_PDF_LINK_PAGE))
    respx.get(_PDF_URL).mock(
        return_value=httpx.Response(200, content=_load_pdf("sf_dept_listing.pdf"))
    )

    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) >= 30
    assert dept_map["301"] == "Christine Van Aken"
    assert dept_map["9"] == "Simon J. Frankel"


@respx.mock
def test_fetch_mapping_falls_back_to_known_url_when_home_unavailable() -> None:
    respx.get(SF_HOME_URL).mock(return_value=httpx.Response(503))
    respx.get(SF_ASSIGNMENTS_URL).mock(
        return_value=httpx.Response(200, text=_load_html("sf_judicial_assignments_2026.html"))
    )
    dept_map = fetch_department_judge_mapping()
    assert dept_map["206"] == "Rochelle East"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors from the assignments page."""
    respx.get(SF_HOME_URL).mock(return_value=httpx.Response(503))
    respx.get(SF_ASSIGNMENTS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


@respx.mock
def test_fetch_mapping_access_denied_redirect_raises_clear_error() -> None:
    """The old URL 301s to /system/403 (served as HTTP 200) -- fail loudly, naming it."""
    respx.get(SF_HOME_URL).mock(return_value=httpx.Response(503))
    respx.get(SF_ASSIGNMENTS_URL).mock(
        return_value=httpx.Response(301, headers={"Location": "/system/403"})
    )
    respx.get("https://sf.courts.ca.gov/system/403").mock(
        return_value=httpx.Response(200, text="<html>Access denied</html>")
    )
    with pytest.raises(ValueError, match="/system/403"):
        fetch_department_judge_mapping()


@respx.mock
def test_fetch_mapping_no_tables_and_no_pdf_raises_clear_error() -> None:
    respx.get(SF_HOME_URL).mock(
        return_value=httpx.Response(200, text=_load_html("sf_home_nav.html"))
    )
    respx.get(_LIVE_ASSIGNMENTS_URL).mock(
        return_value=httpx.Response(200, text="<html><body><p>Coming soon</p></body></html>")
    )
    with pytest.raises(ValueError) as excinfo:
        fetch_department_judge_mapping()
    msg = str(excinfo.value)
    assert _LIVE_ASSIGNMENTS_URL in msg
    assert "DEPT" in msg and "PDF" in msg


@respx.mock
def test_fetch_mapping_pdf_with_zero_departments_raises() -> None:
    respx.get(SF_HOME_URL).mock(
        return_value=httpx.Response(200, text=_load_html("sf_home_nav.html"))
    )
    respx.get(_LIVE_ASSIGNMENTS_URL).mock(return_value=httpx.Response(200, text=_PDF_LINK_PAGE))
    respx.get(_PDF_URL).mock(return_value=httpx.Response(200, content=b"%PDF-1.4 not a real pdf"))
    with pytest.raises(ValueError, match="0 departments"):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# SFCourtDirectory -- fetch_current / snapshots
# ---------------------------------------------------------------------------


def _mock_db_conn() -> MagicMock:
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.return_value = None
    return mock_conn


class TestSFCourtDirectory:
    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        """fetch_current returns the archived assignments HTML and a valid mapping."""
        _mock_live_site()
        directory = SFCourtDirectory(
            s3_client=MagicMock(), s3_bucket="test-bucket", db_conn=MagicMock()
        )

        raw, mapping = directory.fetch_current()

        assert isinstance(raw, bytes)
        assert b"<article" in raw
        # Re-parsing the archived bytes reproduces the mapping (S3 is source of truth).
        assert build_department_judge_map(parse_assignments_html(raw.decode())) == mapping
        assert len(mapping) >= 30
        assert mapping["301"] == "Christine Van Aken"

    def test_court_id(self) -> None:
        assert SFCourtDirectory.COURT_ID == "ca_san_francisco"

    @respx.mock
    def test_fetch_and_snapshot_defaults_court_id(self) -> None:
        _mock_live_site()
        mock_s3 = MagicMock()
        directory = SFCourtDirectory(
            s3_client=mock_s3, s3_bucket="test-bucket", db_conn=_mock_db_conn()
        )

        with (
            patch("ingestion.db.clear_roster_cache"),
            patch(
                "ingestion.judge_seed.seed_judges_from_directory_snapshots",
                return_value={"inserted": 0},
            ),
        ):
            mapping = directory.fetch_and_snapshot()

        assert len(mapping) >= 30
        assert mapping["301"] == "Christine Van Aken"
        head_call = mock_s3.head_object.call_args
        assert head_call.kwargs["Key"].startswith("directories/ca_san_francisco/")

    def test_save_snapshot_html_uses_base_class(self) -> None:
        """HTML snapshots go through the base class (text/html, .html key)."""
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        directory = SFCourtDirectory(
            s3_client=mock_s3, s3_bucket="test-bucket", db_conn=_mock_db_conn()
        )
        with (
            patch("ingestion.db.clear_roster_cache") as clear_cache,
            patch(
                "ingestion.judge_seed.seed_judges_from_directory_snapshots",
                return_value={"inserted": 0},
            ),
        ):
            directory.save_snapshot(b"<article>x</article>", {"301": "Test"}, "ca_san_francisco")

        put_call = mock_s3.put_object.call_args
        assert put_call.kwargs["ContentType"] == "text/html"
        assert put_call.kwargs["Key"].endswith(".html")
        clear_cache.assert_called_once()

    def test_save_snapshot_uses_pdf_content_type(self) -> None:
        """PDF snapshots (legacy fallback) upload with application/pdf content type."""
        pdf_bytes = _load_pdf("sf_dept_listing.pdf")
        mock_s3 = MagicMock()
        directory = SFCourtDirectory(
            s3_client=mock_s3, s3_bucket="test-bucket", db_conn=_mock_db_conn()
        )

        directory.save_snapshot(pdf_bytes, {"301": "Test Judge"}, "ca_san_francisco")

        put_call = mock_s3.put_object.call_args
        assert put_call.kwargs["ContentType"] == "application/pdf"
        assert put_call.kwargs["Key"].endswith(".pdf")
