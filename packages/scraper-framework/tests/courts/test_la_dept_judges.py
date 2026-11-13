"""Tests for LA department-to-judge mapping — built against real-format fixtures.

Fixtures:
    la_judicial_officers.html — representative HTML table matching the structure
    of the retired https://www.lacourt.ca.gov/judicialofficers/ui/SearchResult.aspx
    (still parsed for archived S3 snapshots).
    la_judicial_directory_trimmed.pdf — six pages of the real weekly "Judicial
    Assignment and Directory" PDF (Week of 11/9/2026): Stanley Mosk p1, East LA /
    Metropolitan / Edelman p5, Compton / Norwalk p20, Santa Monica p25, the first
    alphabetical index page, and the location legend (#4874).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from botocore.exceptions import ClientError

from courts.ca.la_dept_judges import (
    JUDICIAL_DIRECTORY_PDF_URL,
    DirectoryOfficer,
    JudicialOfficer,
    LACourtDirectory,
    _parse_combined_name,
    build_department_judge_map,
    clean_officer_name,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    normalize_department,
    parse_judicial_directory_pdf,
    parse_judicial_officers_html,
)
from courts.ca.la_tentatives import LATentativeRulingsScraper, default_config
from framework import ContentFormat, EmptyListing
from framework.models import CapturedDocument

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


DIRECTORY_PDF = "la_judicial_directory_trimmed.pdf"


def _load_pdf() -> bytes:
    return (FIXTURES / DIRECTORY_PDF).read_bytes()


@pytest.fixture(scope="module")
def directory_officers() -> list[DirectoryOfficer]:
    return parse_judicial_directory_pdf(_load_pdf())


# ---------------------------------------------------------------------------
# normalize_department — unit tests
# ---------------------------------------------------------------------------


class TestNormalizeDepartment:
    def test_strips_leading_zeros(self) -> None:
        assert normalize_department("052") == "52"

    def test_strips_leading_zeros_single_digit(self) -> None:
        assert normalize_department("005") == "5"

    def test_no_change_for_unpadded(self) -> None:
        assert normalize_department("3") == "3"

    def test_alphanumeric_unchanged(self) -> None:
        assert normalize_department("F46") == "F46"

    def test_alpha_only_unchanged(self) -> None:
        assert normalize_department("H") == "H"

    def test_zero_stays_zero(self) -> None:
        assert normalize_department("0") == "0"

    def test_whitespace_stripped(self) -> None:
        assert normalize_department(" F46 ") == "F46"

    # ---------------------------------------------------------------------
    # Alphanumeric dept codes — locked in for #2602
    # ---------------------------------------------------------------------
    # Ventura (J6), SB (S*), Riverside (M*, PS*, C*, MV*), OC (CX*, CM*, N*,
    # W*, L*) all share this helper via import.  The helper only strips zeros
    # from *purely numeric* strings (isdigit() check), so alphanumerics pass
    # through unchanged — which is what the dept-to-judge map keys expect.
    # Do not add letter-stripping here; LA County's letter+digits ->
    # letter-only normalization lives in ingestion.department_normalize and
    # must stay out of this helper (see the Ventura import site in
    # ``courts/ca/ventura_dept_judges.py``).

    def test_issue_2602_j6(self) -> None:
        """Exact case from #2602 — Ventura J6 must pass through."""
        assert normalize_department("J6") == "J6"

    @pytest.mark.parametrize(
        "raw",
        [
            "J6",
            "S1",
            "M4",
            "M205",
            "M301",
            "M302",
            "MV1",
            "C1",
            "C3",
            "N1",
            "N15",
            "W5",
            "CM01",
            "CM7",
            "L10",
            "PS1",
            "PS2",
            "PS4",
            "CX02",
            "T1",
        ],
    )
    def test_alphanumeric_codes_preserved(self, raw: str) -> None:
        """All alphanumeric codes from non-LA counties pass through
        unchanged."""
        assert normalize_department(raw) == raw

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("020", "20"),
            ("007", "7"),
            ("001", "1"),
            ("0", "0"),
            ("00", "0"),
        ],
    )
    def test_numeric_zero_stripping_still_works(self, raw: str, expected: str) -> None:
        """AC #4: numeric zero-stripping still works for numeric-only codes."""
        assert normalize_department(raw) == expected


# ---------------------------------------------------------------------------
# _parse_combined_name — unit tests
# ---------------------------------------------------------------------------


class TestParseCombinedName:
    def test_standard_name(self) -> None:
        assert _parse_combined_name("Abeles, Jerrold") == ("Abeles", "Jerrold")

    def test_hyphenated_last_name(self) -> None:
        assert _parse_combined_name("Duffy-Lewis, Kerry") == ("Duffy-Lewis", "Kerry")

    def test_middle_initial(self) -> None:
        assert _parse_combined_name("Crowfoot, William A.") == ("Crowfoot", "William A.")

    def test_no_comma_returns_whole_as_last(self) -> None:
        assert _parse_combined_name("SingleName") == ("SingleName", "")

    def test_extra_whitespace_stripped(self) -> None:
        assert _parse_combined_name("  Kim ,  Helen I. ") == ("Kim", "Helen I.")

    def test_multiple_commas_splits_on_first(self) -> None:
        last, first = _parse_combined_name("Smith, Jr., John")
        assert last == "Smith"
        assert first == "Jr., John"


# ---------------------------------------------------------------------------
# parse_judicial_officers_html — against fixture
# ---------------------------------------------------------------------------


class TestParseJudicialOfficersHtml:
    def test_parses_all_officers(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        assert len(officers) == 14

    def test_first_officer_fields(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        first = officers[0]
        assert first.last_name == "Abeles"
        assert first.first_name == "Jerrold"
        assert first.title == "Judge"
        assert first.courthouse == "Stanley Mosk"
        assert first.department == "052"
        assert first.primary_assignment == "Civil"

    def test_full_name_property(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        first = officers[0]
        assert first.full_name == "Jerrold Abeles"

    def test_commissioner_found(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        commissioners = [o for o in officers if o.title == "Commissioner"]
        assert len(commissioners) >= 1

    def test_empty_table_returns_empty(self) -> None:
        html = (
            "<html><body><table class='joresultstable'>"
            "<thead><tr><th>A</th></tr></thead>"
            "</table></body></html>"
        )
        officers = parse_judicial_officers_html(html)
        assert officers == []

    def test_empty_legacy_table_returns_empty(self) -> None:
        html = (
            "<html><body><table id='GridView1'>"
            "<thead><tr><th>A</th></tr></thead>"
            "</table></body></html>"
        )
        officers = parse_judicial_officers_html(html)
        assert officers == []

    def test_no_table_returns_empty(self) -> None:
        html = "<html><body><p>No table here</p></body></html>"
        officers = parse_judicial_officers_html(html)
        assert officers == []

    def test_crowfoot_in_alhambra(self) -> None:
        """Crowfoot should be in Alhambra Courthouse Dept 3 — matches LA scraper fixture."""
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        crowfoot = [o for o in officers if o.last_name == "Crowfoot"]
        assert len(crowfoot) == 1
        assert crowfoot[0].department == "3"
        assert crowfoot[0].courthouse == "Alhambra Courthouse"

    def test_legacy_format_backward_compatible(self) -> None:
        """Parser should still handle the old GridView1 table format."""
        legacy_html = (
            "<html><body><table id='GridView1'>"
            "<thead><tr>"
            "<th>Last Name</th><th>First Name</th><th>Title</th>"
            "<th>Courthouse</th><th>Department</th><th>Phone</th>"
            "<th>Primary Assignment</th>"
            "</tr></thead>"
            "<tbody><tr>"
            "<td>Smith</td><td>John</td><td>Judge</td>"
            "<td>Central</td><td>042</td><td>(555) 123-4567</td>"
            "<td>Civil</td>"
            "</tr></tbody></table></body></html>"
        )
        officers = parse_judicial_officers_html(legacy_html)
        assert len(officers) == 1
        assert officers[0].last_name == "Smith"
        assert officers[0].first_name == "John"
        assert officers[0].courthouse == "Central"
        assert officers[0].department == "042"

    def test_location_link_text_extracted(self) -> None:
        """Location column may contain <a> tags — text should be extracted."""
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        # Crowfoot's location is wrapped in an <a> tag
        crowfoot = [o for o in officers if o.last_name == "Crowfoot"]
        assert len(crowfoot) == 1
        assert crowfoot[0].courthouse == "Alhambra Courthouse"


# ---------------------------------------------------------------------------
# build_department_judge_map — unit tests
# ---------------------------------------------------------------------------


class TestBuildDepartmentJudgeMap:
    def test_builds_from_fixture(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(officers)
        assert len(dept_map) == 14

    def test_normalizes_zero_padded_depts(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(officers)
        # "052" in the table should be accessible as "52"
        assert "52" in dept_map
        assert dept_map["52"] == "Jerrold Abeles"

    def test_alphanumeric_dept_preserved(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(officers)
        assert "F46" in dept_map
        assert dept_map["F46"] == "Kerry Duffy-Lewis"

    def test_single_char_dept(self) -> None:
        html = _load("la_judicial_officers.html")
        officers = parse_judicial_officers_html(html)
        dept_map = build_department_judge_map(officers)
        assert "H" in dept_map
        assert dept_map["H"] == "Maria Elena Garcia"

    def test_duplicate_dept_keeps_first(self) -> None:
        """If two officers share a dept, the first wins."""
        officers = [
            JudicialOfficer(
                last_name="Smith",
                first_name="John",
                title="Judge",
                courthouse="Test",
                department="10",
                phone="",
                primary_assignment="Civil",
            ),
            JudicialOfficer(
                last_name="Jones",
                first_name="Jane",
                title="Judge",
                courthouse="Test",
                department="10",
                phone="",
                primary_assignment="Civil",
            ),
        ]
        dept_map = build_department_judge_map(officers)
        assert dept_map["10"] == "John Smith"


# ---------------------------------------------------------------------------
# lookup_judge_for_department — unit tests
# ---------------------------------------------------------------------------


class TestLookupJudgeForDepartment:
    def test_found_with_normalized_key(self) -> None:
        dept_map = {"52": "Jerrold Abeles", "3": "William A. Crowfoot"}
        assert lookup_judge_for_department(dept_map, "52") == "Jerrold Abeles"

    def test_found_with_padded_input(self) -> None:
        """Lookup normalizes the input department too."""
        dept_map = {"52": "Jerrold Abeles"}
        assert lookup_judge_for_department(dept_map, "052") == "Jerrold Abeles"

    def test_not_found_returns_none(self) -> None:
        dept_map = {"52": "Jerrold Abeles"}
        assert lookup_judge_for_department(dept_map, "99") is None

    def test_alpha_dept_found(self) -> None:
        dept_map = {"F46": "Kerry Duffy-Lewis"}
        assert lookup_judge_for_department(dept_map, "F46") == "Kerry Duffy-Lewis"


# ---------------------------------------------------------------------------
# clean_officer_name — directory PDF officer cells (#4874)
# ---------------------------------------------------------------------------


class TestCleanOfficerName:
    @pytest.mark.parametrize(
        ("cell", "expected"),
        [
            ("Judge William H. Forman", ("Judge", "William H. Forman")),
            ("Commissioner Naomi R. Cohen", ("Commissioner", "Naomi R. Cohen")),
            ("Judge Shelley Kaufman (SJ)", ("Judge", "Shelley Kaufman")),
            ("Judge Jonathan L. Rosenbloom (ASJ)", ("Judge", "Jonathan L. Rosenbloom")),
            ("Judge Annabelle G. Cortez (STJ)", ("Judge", "Annabelle G. Cortez")),
            # Multi-line role note, joined from two PDF lines.
            ("Judge Brenda J. Penny (SJ- Probate/Mental Health)", ("Judge", "Brenda J. Penny")),
            ("Judge Akemi Arakaki (SJ-Juvenile Del/Dep)", ("Judge", "Akemi Arakaki")),
            # A note left open (its second line missing) is still stripped.
            ("Judge Brenda J. Penny (SJ-", ("Judge", "Brenda J. Penny")),
            # Generational suffixes stay at the end.
            ("Judge Charles Q. Clay III", ("Judge", "Charles Q. Clay III")),
            ("Judge Edward B. Moreton Jr.", ("Judge", "Edward B. Moreton Jr.")),
            ("Judge Sergio C. Tapia II", ("Judge", "Sergio C. Tapia II")),
            ("  Judge   Ruben   Garcia ", ("Judge", "Ruben Garcia")),
        ],
    )
    def test_cleans_cell(self, cell: str, expected: tuple[str, str]) -> None:
        assert clean_officer_name(cell) == expected

    @pytest.mark.parametrize(
        "cell", ["", "Judicial Officer", "California 90012", "Judge", "Presiding Judge's Office"]
    )
    def test_non_officer_cell_returns_empty(self, cell: str) -> None:
        assert clean_officer_name(cell) == ("", "")


# ---------------------------------------------------------------------------
# parse_judicial_directory_pdf — real weekly PDF pages (#4874)
# ---------------------------------------------------------------------------


def _by_dept(officers: list[DirectoryOfficer], dept: str) -> list[DirectoryOfficer]:
    return [o for o in officers if o.department == dept]


class TestParseJudicialDirectoryPdf:
    def test_parses_every_officer_row(self, directory_officers: list[DirectoryOfficer]) -> None:
        # 26 Stanley Mosk + 21 East LA/Metropolitan/Edelman + 23 Compton/Norwalk
        # + 8 Santa Monica rows. The index and legend pages contribute nothing.
        assert len(directory_officers) == 78

    def test_first_row_fields(self, directory_officers: list[DirectoryOfficer]) -> None:
        assert directory_officers[0] == DirectoryOfficer(
            department="215",
            full_name="Shelley Kaufman",  # "(SJ)" stripped
            title="Judge",
            courthouse="Stanley Mosk",
            region="CENTRAL",
            primary_assignment="Family Law",
        )

    def test_multi_officer_department_keeps_every_row(
        self, directory_officers: list[DirectoryOfficer]
    ) -> None:
        dept_217 = _by_dept(directory_officers, "217")
        # The first officer's "(SJ- / Probate/Mental Health)" note wraps onto a second line.
        assert [o.full_name for o in dept_217] == ["Brenda J. Penny", "Kristen A. Williams"]
        assert [o.full_name for o in _by_dept(directory_officers, "245")] == [
            "Naomi R. Cohen",
            "Sarah Reisman",
        ]
        assert {o.title for o in _by_dept(directory_officers, "245")} == {"Commissioner"}

    def test_wrapped_name_is_joined(self, directory_officers: list[DirectoryOfficer]) -> None:
        # "Commissioner LaTonya Hadnot" / "Prioleau" spans two PDF lines.
        [officer] = _by_dept(directory_officers, "105")
        assert officer.full_name == "LaTonya Hadnot Prioleau"
        assert officer.courthouse == "Compton"

    def test_wrapped_role_note_is_stripped(
        self, directory_officers: list[DirectoryOfficer]
    ) -> None:
        names = [o.full_name for o in _by_dept(directory_officers, "400")]
        assert "Akemi Arakaki" in names  # "(SJ-Juvenile" / "Del/Dep)"
        assert "Annabelle G. Cortez" in names  # "(STJ)"

    def test_no_title_or_parenthetical_left_in_names(
        self, directory_officers: list[DirectoryOfficer]
    ) -> None:
        for officer in directory_officers:
            assert "(" not in officer.full_name and ")" not in officer.full_name
            assert not officer.full_name.startswith(("Judge ", "Commissioner "))
            assert len(officer.full_name.split()) >= 2

    def test_rows_attributed_to_their_courthouse(
        self, directory_officers: list[DirectoryOfficer]
    ) -> None:
        courthouses = {o.courthouse for o in _by_dept(directory_officers, "400")}
        assert courthouses == {"Metropolitan", "Edmund D. Edelman Children's Court"}
        # Norwalk's block starts mid-page after a SOUTHEAST region heading.
        norwalk = [o for o in directory_officers if o.courthouse == "Norwalk"]
        assert [o.department for o in norwalk] == ["A", "B", "C", "D"]
        assert {o.region for o in norwalk} == {"SOUTHEAST"}

    def test_row_without_phone_or_room(self, directory_officers: list[DirectoryOfficer]) -> None:
        # Santa Monica "V   V   Judge Robert S. Draper Civil" has no phone column.
        [officer] = _by_dept(directory_officers, "V")
        assert officer.full_name == "Robert S. Draper"
        assert officer.primary_assignment == "Civil"

    def test_banner_index_and_legend_are_ignored(
        self, directory_officers: list[DirectoryOfficer]
    ) -> None:
        names = {o.full_name for o in directory_officers}
        # Presiding Judge banner line on every page.
        assert not any("Tapia" in n for n in names)
        # Index-only officers (page 26 of the full PDF).
        assert not any("Berle" in n or "Bershon" in n for n in names)

    def test_invalid_pdf_returns_empty(self) -> None:
        assert parse_judicial_directory_pdf(b"%PDF-1.4 not really a pdf") == []

    def test_mapping_from_pdf(self, directory_officers: list[DirectoryOfficer]) -> None:
        dept_map = build_department_judge_map(directory_officers, prefer_assignment="Civil")
        assert len(dept_map) == 62
        # Unpadded keys, like the HTML-era mapping.
        assert dept_map["14"] == "Ashley Price"
        assert dept_map["71"] == "Rene C. Gilbertson"
        assert "014" not in dept_map
        # Multi-officer departments keep the first officer listed.
        assert dept_map["217"] == "Brenda J. Penny"
        assert dept_map["245"] == "Naomi R. Cohen"
        assert dept_map["601"] == "Jacqueline Phillips"
        assert dept_map["215"] == "Shelley Kaufman"
        assert dept_map["308"] == "Charles Q. Clay III"

    def test_civil_officer_wins_department_collision(
        self, directory_officers: list[DirectoryOfficer]
    ) -> None:
        dept_map = build_department_judge_map(directory_officers, prefer_assignment="Civil")
        # "M": Compton Family Law listed first; Santa Monica Civil wins.
        assert dept_map["M"] == "Mark A. Young"
        # "C": Compton Criminal listed first; Norwalk Civil wins.
        assert dept_map["C"] == "Lee W. Tsao"
        # "A": both Compton and Norwalk listed; the first Civil one (Compton) wins.
        assert dept_map["A"] == "Elizabeth L. Bradley"
        # No Civil officer anywhere: first listed wins.
        assert dept_map["400"] == "David J. Cowan"

    def test_without_preference_first_listed_wins(
        self, directory_officers: list[DirectoryOfficer]
    ) -> None:
        dept_map = build_department_judge_map(directory_officers)
        assert dept_map["M"] == "Kimberly Dotson"
        assert dept_map["C"] == "Kelvin D. Filer"


# ---------------------------------------------------------------------------
# fetch_department_judge_mapping — mocked HTTP
# ---------------------------------------------------------------------------


def _mock_directory_pdf(status: int = 200, content: bytes | None = None) -> None:
    respx.get(JUDICIAL_DIRECTORY_PDF_URL).mock(
        return_value=httpx.Response(
            status,
            content=_load_pdf() if content is None else content,
            headers={"content-type": "application/pdf"},
        )
    )


def test_directory_url_is_weekly_pdf() -> None:
    assert JUDICIAL_DIRECTORY_PDF_URL == (
        "http://mycourtservices.lacourt.org:90/lineupreports/judicial%20directory.pdf"
    )


@respx.mock
def test_fetch_mapping_with_fixture() -> None:
    """fetch_department_judge_mapping returns a correct map from the fixture PDF."""
    _mock_directory_pdf()
    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) == 62
    assert dept_map["224"] == "William H. Forman"
    # Civil preference is applied on the live path.
    assert dept_map["M"] == "Mark A. Young"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    _mock_directory_pdf(status=503, content=b"")
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


@respx.mock
def test_fetch_mapping_non_pdf_response_raises() -> None:
    """An HTML page served with 200 (moved report) fails loudly, naming the URL."""
    respx.get(JUDICIAL_DIRECTORY_PDF_URL).mock(
        return_value=httpx.Response(200, text="<html>Not Found</html>")
    )
    with pytest.raises(ValueError, match="not a PDF"):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# LACourtDirectory — fetch_current and snapshot integration
# ---------------------------------------------------------------------------


def _head_object_404(**kwargs: Any) -> None:
    """Simulate S3 HeadObject returning 404 (object not found)."""
    raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


class TestLACourtDirectory:
    """Tests for the LACourtDirectory subclass of CourtDirectory."""

    def _make_mock_db(self) -> MagicMock:
        """Create a mock psycopg connection with cursor context manager."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None  # no prior snapshot (not duplicate)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn

    def _make_mock_s3(self) -> MagicMock:
        """Create a mock S3 client with HeadObject returning 404 (not found)."""
        mock = MagicMock()
        mock.head_object.side_effect = _head_object_404
        return mock

    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        """fetch_current() returns (raw_pdf, dept_map) from the live PDF."""
        _mock_directory_pdf()

        directory = LACourtDirectory(
            s3_client=self._make_mock_s3(),
            s3_bucket="test-bucket",
            db_conn=self._make_mock_db(),
        )

        raw, mapping = directory.fetch_current()
        assert isinstance(raw, bytes)
        assert len(raw) > 0
        assert isinstance(mapping, dict)
        assert len(mapping) == 62
        assert mapping["224"] == "William H. Forman"
        assert mapping["105"] == "LaTonya Hadnot Prioleau"

    @respx.mock
    def test_fetch_and_snapshot_archives_to_s3(self) -> None:
        """fetch_and_snapshot() uploads the raw PDF to S3."""
        _mock_directory_pdf()

        mock_s3 = self._make_mock_s3()
        mock_db = self._make_mock_db()

        directory = LACourtDirectory(
            s3_client=mock_s3,
            s3_bucket="test-bucket",
            db_conn=mock_db,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) == 62
        assert mapping["224"] == "William H. Forman"

        # Verify S3 put_object was called
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args
        assert call_kwargs.kwargs["Bucket"] == "test-bucket"
        assert call_kwargs.kwargs["Key"].startswith("directories/ca_los_angeles/")
        assert call_kwargs.kwargs["ContentType"] == "application/pdf"
        assert call_kwargs.kwargs["Key"].endswith(".pdf")
        assert len(call_kwargs.kwargs["Body"]) > 0

    @respx.mock
    def test_fetch_and_snapshot_inserts_into_db(self) -> None:
        """fetch_and_snapshot() inserts mapping into the DB when content is new."""
        _mock_directory_pdf()

        mock_s3 = self._make_mock_s3()
        mock_db = self._make_mock_db()
        mock_cursor = mock_db.cursor.return_value.__enter__.return_value

        directory = LACourtDirectory(
            s3_client=mock_s3,
            s3_bucket="test-bucket",
            db_conn=mock_db,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) == 62

        # The cursor should have been called for both _is_duplicate check
        # and the INSERT.  The INSERT call contains the mapping JSON.
        calls = mock_cursor.execute.call_args_list
        assert len(calls) >= 1

        # Verify commit was called (new snapshot inserted)
        mock_db.commit.assert_called()

    @respx.mock
    def test_fetch_and_snapshot_deduplicates(self) -> None:
        """fetch_and_snapshot() skips DB insert when content hash matches."""
        _mock_directory_pdf()

        mock_s3 = self._make_mock_s3()
        mock_db = self._make_mock_db()
        mock_cursor = mock_db.cursor.return_value.__enter__.return_value

        # Simulate existing snapshot with same content hash
        from framework.hashing import sha256_hex

        content_hash = sha256_hex(_load_pdf())
        mock_cursor.fetchone.return_value = (content_hash,)

        directory = LACourtDirectory(
            s3_client=mock_s3,
            s3_bucket="test-bucket",
            db_conn=mock_db,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) == 62

        # S3 upload happens because HeadObject returns 404 (content-addressed key not yet in S3)
        mock_s3.put_object.assert_called_once()

        # But commit should NOT be called (duplicate detected in DB)
        mock_db.commit.assert_not_called()

    @respx.mock
    def test_fetch_current_http_error_raises(self) -> None:
        """fetch_current() propagates HTTP errors."""
        _mock_directory_pdf(status=503, content=b"")

        directory = LACourtDirectory(
            s3_client=self._make_mock_s3(),
            s3_bucket="test-bucket",
            db_conn=self._make_mock_db(),
        )

        with pytest.raises(httpx.HTTPStatusError):
            directory.fetch_current()

    @respx.mock
    def test_fetch_and_snapshot_never_saves_empty_roster(self) -> None:
        """A PDF that parses to 0 departments is not archived or inserted (#4880)."""
        import pymupdf

        blank = pymupdf.open()
        blank.new_page()
        _mock_directory_pdf(content=blank.tobytes())

        mock_s3 = self._make_mock_s3()
        mock_db = self._make_mock_db()
        directory = LACourtDirectory(s3_client=mock_s3, s3_bucket="test-bucket", db_conn=mock_db)

        assert directory.fetch_and_snapshot() == {}
        mock_s3.put_object.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_court_id_default(self) -> None:
        """LACourtDirectory.COURT_ID is 'ca_los_angeles'."""
        assert LACourtDirectory.COURT_ID == "ca_los_angeles"

    @respx.mock
    def test_fetch_and_snapshot_uses_default_court_id(self) -> None:
        """fetch_and_snapshot() without args uses COURT_ID."""
        _mock_directory_pdf()

        mock_s3 = self._make_mock_s3()
        mock_db = self._make_mock_db()

        directory = LACourtDirectory(
            s3_client=mock_s3,
            s3_bucket="test-bucket",
            db_conn=mock_db,
        )

        directory.fetch_and_snapshot()

        # Verify S3 key uses ca_los_angeles
        call_kwargs = mock_s3.put_object.call_args
        assert "ca_los_angeles" in call_kwargs.kwargs["Key"]


# ---------------------------------------------------------------------------
# Scraper integration: court_directory snapshots during fetch_documents
# ---------------------------------------------------------------------------


class TestScraperCourtDirectoryIntegration:
    """Test that LATentativeRulingsScraper snapshots the directory when given
    a CourtDirectory instance."""

    @respx.mock
    def test_fetch_documents_snapshots_directory(self) -> None:
        """When court_directory is provided, fetch_documents calls
        fetch_and_snapshot and populates dept_judge_map."""
        _mock_directory_pdf()

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = _head_object_404
        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_db.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_db.cursor.return_value.__exit__ = MagicMock(return_value=False)

        directory = LACourtDirectory(
            s3_client=mock_s3,
            s3_bucket="test-bucket",
            db_conn=mock_db,
        )

        config = default_config()
        scraper = LATentativeRulingsScraper(
            config=config,
            court_directory=directory,
        )

        # Mock the main page fetch to return an empty page (no options)
        # so we only test the directory snapshotting, not the full scrape
        empty_page = (
            "<html><body>"
            '<input name="__VIEWSTATE" value="abc"/>'
            '<select id="siteMasterHolder_basicBodyHolder_List2DeptDate">'
            "</select>"
            "</body></html>"
        )
        respx.get("https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil").mock(
            return_value=httpx.Response(200, text=empty_page)
        )

        # An option-less page with no empty-state marker is an empty
        # listing (#4736); the snapshot happens before that check.
        with pytest.raises(EmptyListing):
            scraper.fetch_documents()

        # Verify directory was snapshotted (S3 upload happened)
        mock_s3.put_object.assert_called_once()

        # Verify the dept_judge_map was populated
        assert len(scraper._dept_judge_map) == 62
        assert scraper._dept_judge_map["224"] == "William H. Forman"


# ---------------------------------------------------------------------------
# Integration: LA scraper should use mapping to populate judge_name
# ---------------------------------------------------------------------------


def test_mapping_matches_la_scraper_departments() -> None:
    """Verify the mapping covers departments seen in the LA scraper fixtures.

    The LA scraper fixture uses Alhambra Courthouse Dept 3, which should
    map to William A. Crowfoot in our judicial officers fixture.
    """
    html = _load("la_judicial_officers.html")
    officers = parse_judicial_officers_html(html)
    dept_map = build_department_judge_map(officers)

    # Dept 3 (Alhambra) — from la_ruling_response.html fixture
    assert lookup_judge_for_department(dept_map, "3") == "William A. Crowfoot"

    # Dept F46 (Chatsworth) — from la_ruling_cha_f46.html fixture
    assert lookup_judge_for_department(dept_map, "F46") == "Kerry Duffy-Lewis"

    # Dept A (Compton) — from la_ruling_com_a.html fixture
    assert lookup_judge_for_department(dept_map, "A") == "Jared D. Moses"

    # Dept 205 (Beverly Hills) — from la_ruling_bh205.html fixture
    assert lookup_judge_for_department(dept_map, "205") == "Helen I. Kim"

    # Dept P (Pasadena) — from la_ruling_pas_p.html fixture
    assert lookup_judge_for_department(dept_map, "P") == "Daniel J. Palazuelos"


# ---------------------------------------------------------------------------
# Scraper integration: parse_document fallback from dept_judge_map
# ---------------------------------------------------------------------------


class TestScraperDeptJudgeMapFallback:
    """Test that LATentativeRulingsScraper.parse_document uses the dept-judge
    mapping as a fallback when the ruling text doesn't contain a judge name."""

    def _make_doc_without_judge(self, department: str = "52") -> CapturedDocument:
        """Create a ruling doc whose HTML has no judge name signature."""
        html = (
            '<html><body><div id="speechSynthesis">'
            "<p><B>Case Number:</B> 24STCV12345</p>"
            "<p>The motion for summary judgment is GRANTED.</p>"
            "</div></body></html>"
        )
        from datetime import datetime

        return CapturedDocument(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=datetime(2026, 3, 2, 18, 0, 0),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="",
            department=department,
        )

    def test_populates_judge_from_mapping_when_text_has_none(self) -> None:
        """When ruling text lacks a judge name, use dept mapping."""
        dept_map = {"52": "Jerrold Abeles", "3": "William A. Crowfoot"}
        config = default_config()
        scraper = LATentativeRulingsScraper(config=config, dept_judge_map=dept_map)

        doc = self._make_doc_without_judge(department="52")
        result = scraper.parse_document(doc)
        assert result.judge_name == "Jerrold Abeles"

    def test_does_not_override_judge_from_text(self) -> None:
        """When ruling text contains a judge name, don't override with mapping."""
        html = (
            '<html><body><div id="speechSynthesis">'
            "<p><B>Case Number:</B> 24STCV12345</p>"
            "<p>The motion is GRANTED.</p>"
            "<div>Sarah Johnson Judge of the Superior Court</div>"
            "</div></body></html>"
        )
        from datetime import datetime

        doc = CapturedDocument(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=datetime(2026, 3, 2, 18, 0, 0),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="",
            department="52",
        )

        dept_map = {"52": "Jerrold Abeles"}
        config = default_config()
        scraper = LATentativeRulingsScraper(config=config, dept_judge_map=dept_map)

        result = scraper.parse_document(doc)
        assert result.judge_name == "Sarah Johnson"

    def test_no_mapping_leaves_judge_none(self) -> None:
        """Without a dept_judge_map, judge_name stays None."""
        config = default_config()
        scraper = LATentativeRulingsScraper(config=config)

        doc = self._make_doc_without_judge(department="52")
        result = scraper.parse_document(doc)
        assert result.judge_name is None

    def test_unknown_dept_leaves_judge_none(self) -> None:
        """If department not in mapping, judge_name stays None."""
        dept_map = {"3": "William A. Crowfoot"}
        config = default_config()
        scraper = LATentativeRulingsScraper(config=config, dept_judge_map=dept_map)

        doc = self._make_doc_without_judge(department="999")
        result = scraper.parse_document(doc)
        assert result.judge_name is None
