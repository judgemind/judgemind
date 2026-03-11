"""Tests for LA department-to-judge mapping — built against real-format fixture HTML.

Fixtures:
    la_judicial_officers.html — representative HTML table matching the structure
    of https://www.lacourt.ca.gov/judicialofficers/ui/SearchResult.aspx
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from courts.ca.la_dept_judges import (
    JUDICIAL_OFFICERS_URL,
    JudicialOfficer,
    LACourtDirectory,
    build_department_judge_map,
    fetch_department_judge_mapping,
    lookup_judge_for_department,
    normalize_department,
    parse_judicial_officers_html,
)
from courts.ca.la_tentatives import LATentativeRulingsScraper, default_config
from framework import ContentFormat
from framework.models import CapturedDocument

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


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
# fetch_department_judge_mapping — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_mapping_with_fixture() -> None:
    """fetch_department_judge_mapping returns a correct map from fixture HTML."""
    html = _load("la_judicial_officers.html")
    respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))
    dept_map = fetch_department_judge_mapping()
    assert len(dept_map) == 14
    assert dept_map["3"] == "William A. Crowfoot"
    assert dept_map["52"] == "Jerrold Abeles"


@respx.mock
def test_fetch_mapping_http_error_raises() -> None:
    """fetch_department_judge_mapping raises on HTTP errors."""
    respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_department_judge_mapping()


# ---------------------------------------------------------------------------
# LACourtDirectory — fetch_current and snapshot integration
# ---------------------------------------------------------------------------


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
        """Create a mock S3 client."""
        return MagicMock()

    @respx.mock
    def test_fetch_current_returns_raw_and_mapping(self) -> None:
        """fetch_current() returns (raw_bytes, dept_map) from the live page."""
        html = _load("la_judicial_officers.html")
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))

        directory = LACourtDirectory(
            s3_client=self._make_mock_s3(),
            s3_bucket="test-bucket",
            db_conn=self._make_mock_db(),
        )

        raw, mapping = directory.fetch_current()
        assert isinstance(raw, bytes)
        assert len(raw) > 0
        assert isinstance(mapping, dict)
        assert len(mapping) == 14
        assert mapping["3"] == "William A. Crowfoot"
        assert mapping["52"] == "Jerrold Abeles"

    @respx.mock
    def test_fetch_and_snapshot_archives_to_s3(self) -> None:
        """fetch_and_snapshot() uploads raw HTML to S3."""
        html = _load("la_judicial_officers.html")
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))

        mock_s3 = self._make_mock_s3()
        mock_db = self._make_mock_db()

        directory = LACourtDirectory(
            s3_client=mock_s3,
            s3_bucket="test-bucket",
            db_conn=mock_db,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) == 14
        assert mapping["3"] == "William A. Crowfoot"

        # Verify S3 put_object was called
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args
        assert call_kwargs.kwargs["Bucket"] == "test-bucket"
        assert call_kwargs.kwargs["Key"].startswith("directories/ca_los_angeles/")
        assert call_kwargs.kwargs["ContentType"] == "text/html"
        assert len(call_kwargs.kwargs["Body"]) > 0

    @respx.mock
    def test_fetch_and_snapshot_inserts_into_db(self) -> None:
        """fetch_and_snapshot() inserts mapping into the DB when content is new."""
        html = _load("la_judicial_officers.html")
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))

        mock_s3 = self._make_mock_s3()
        mock_db = self._make_mock_db()
        mock_cursor = mock_db.cursor.return_value.__enter__.return_value

        directory = LACourtDirectory(
            s3_client=mock_s3,
            s3_bucket="test-bucket",
            db_conn=mock_db,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) == 14

        # The cursor should have been called for both _is_duplicate check
        # and the INSERT.  The INSERT call contains the mapping JSON.
        calls = mock_cursor.execute.call_args_list
        assert len(calls) >= 1

        # Verify commit was called (new snapshot inserted)
        mock_db.commit.assert_called()

    @respx.mock
    def test_fetch_and_snapshot_deduplicates(self) -> None:
        """fetch_and_snapshot() skips DB insert when content hash matches."""
        html = _load("la_judicial_officers.html")
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))

        mock_s3 = self._make_mock_s3()
        mock_db = self._make_mock_db()
        mock_cursor = mock_db.cursor.return_value.__enter__.return_value

        # Simulate existing snapshot with same content hash
        from framework.hashing import sha256_hex

        content_hash = sha256_hex(html.encode("utf-8"))
        mock_cursor.fetchone.return_value = (content_hash,)

        directory = LACourtDirectory(
            s3_client=mock_s3,
            s3_bucket="test-bucket",
            db_conn=mock_db,
        )

        mapping = directory.fetch_and_snapshot()
        assert len(mapping) == 14

        # S3 upload should still happen (always archive raw)
        mock_s3.put_object.assert_called_once()

        # But commit should NOT be called (duplicate detected)
        mock_db.commit.assert_not_called()

    @respx.mock
    def test_fetch_current_http_error_raises(self) -> None:
        """fetch_current() propagates HTTP errors."""
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(503))

        directory = LACourtDirectory(
            s3_client=self._make_mock_s3(),
            s3_bucket="test-bucket",
            db_conn=self._make_mock_db(),
        )

        with pytest.raises(httpx.HTTPStatusError):
            directory.fetch_current()

    def test_court_id_default(self) -> None:
        """LACourtDirectory.COURT_ID is 'ca_los_angeles'."""
        assert LACourtDirectory.COURT_ID == "ca_los_angeles"

    @respx.mock
    def test_fetch_and_snapshot_uses_default_court_id(self) -> None:
        """fetch_and_snapshot() without args uses COURT_ID."""
        html = _load("la_judicial_officers.html")
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))

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
        html = _load("la_judicial_officers.html")
        respx.get(JUDICIAL_OFFICERS_URL).mock(return_value=httpx.Response(200, text=html))

        mock_s3 = MagicMock()
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

        scraper.fetch_documents()

        # Verify directory was snapshotted (S3 upload happened)
        mock_s3.put_object.assert_called_once()

        # Verify the dept_judge_map was populated
        assert len(scraper._dept_judge_map) == 14
        assert scraper._dept_judge_map["3"] == "William A. Crowfoot"


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


# ---------------------------------------------------------------------------
# Scraper integration: date-aware directory lookup (#767)
# ---------------------------------------------------------------------------


class TestScraperDateAwareLookup:
    """Test that LATentativeRulingsScraper.parse_document uses date-aware
    snapshot lookups via CourtDirectory.lookup_judge when a court directory
    is available and the ruling has a hearing date."""

    def _make_doc_without_judge(
        self,
        department: str = "52",
        hearing_date: datetime | None = None,
    ) -> CapturedDocument:
        """Create a ruling doc whose HTML has no judge name signature."""
        from datetime import datetime as dt

        html = (
            '<html><body><div id="speechSynthesis">'
            "<p><B>Case Number:</B> 24STCV12345</p>"
            "<p>The motion for summary judgment is GRANTED.</p>"
            "</div></body></html>"
        )
        return CapturedDocument(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=dt(2026, 3, 2, 18, 0, 0),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="",
            department=department,
            hearing_date=hearing_date,
        )

    def _make_directory_with_snapshot(
        self,
        snapshot_mapping: dict[str, str] | None,
    ) -> LACourtDirectory:
        """Create a LACourtDirectory with a mocked get_snapshot response."""
        mock_db = MagicMock()
        mock_cursor = MagicMock()
        if snapshot_mapping is not None:
            mock_cursor.fetchone.return_value = (snapshot_mapping,)
        else:
            mock_cursor.fetchone.return_value = None
        mock_db.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_db.cursor.return_value.__exit__ = MagicMock(return_value=False)

        return LACourtDirectory(
            s3_client=MagicMock(),
            s3_bucket="test-bucket",
            db_conn=mock_db,
        )

    def test_uses_historical_snapshot_for_hearing_date(self) -> None:
        """When court directory is present and ruling has a hearing date,
        use date-aware lookup_judge (not static dept map)."""
        from datetime import datetime as dt

        # Historical snapshot: dept 52 was Judge Alpha in Feb 2026
        snapshot_mapping = {"52": "Judge Alpha"}
        directory = self._make_directory_with_snapshot(snapshot_mapping)

        config = default_config()
        scraper = LATentativeRulingsScraper(
            config=config,
            court_directory=directory,
            # Static map has a different judge — should NOT be used
            dept_judge_map={"52": "Judge Beta"},
        )

        doc = self._make_doc_without_judge(
            department="52",
            hearing_date=dt(2026, 2, 15),
        )
        result = scraper.parse_document(doc)

        assert result.judge_name == "Judge Alpha"

    def test_falls_back_to_static_map_without_hearing_date(self) -> None:
        """When court directory is present but ruling has no hearing date,
        fall back to the static dept_judge_map."""
        snapshot_mapping = {"52": "Judge Alpha"}
        directory = self._make_directory_with_snapshot(snapshot_mapping)

        config = default_config()
        scraper = LATentativeRulingsScraper(
            config=config,
            court_directory=directory,
            dept_judge_map={"52": "Judge Beta"},
        )

        doc = self._make_doc_without_judge(department="52", hearing_date=None)
        result = scraper.parse_document(doc)

        # No hearing date -> can't do date-aware lookup, falls back to static map
        assert result.judge_name == "Judge Beta"

    def test_falls_back_to_static_map_without_court_directory(self) -> None:
        """Without a court directory, the static dept_judge_map is used."""
        from datetime import datetime as dt

        config = default_config()
        scraper = LATentativeRulingsScraper(
            config=config,
            dept_judge_map={"52": "Judge Beta"},
        )

        doc = self._make_doc_without_judge(
            department="52",
            hearing_date=dt(2026, 2, 15),
        )
        result = scraper.parse_document(doc)

        assert result.judge_name == "Judge Beta"

    def test_returns_none_when_snapshot_has_no_department(self) -> None:
        """When the historical snapshot exists but does not contain the dept,
        and no static map is available, judge is None."""
        from datetime import datetime as dt

        snapshot_mapping = {"99": "Judge Other"}
        directory = self._make_directory_with_snapshot(snapshot_mapping)

        config = default_config()
        scraper = LATentativeRulingsScraper(
            config=config,
            court_directory=directory,
        )

        doc = self._make_doc_without_judge(
            department="52",
            hearing_date=dt(2026, 2, 15),
        )
        result = scraper.parse_document(doc)

        assert result.judge_name is None

    def test_normalizes_department_for_lookup(self) -> None:
        """The date-aware lookup normalizes the department (e.g., '052' -> '52')."""
        from datetime import datetime as dt

        snapshot_mapping = {"52": "Judge Alpha"}
        directory = self._make_directory_with_snapshot(snapshot_mapping)

        config = default_config()
        scraper = LATentativeRulingsScraper(
            config=config,
            court_directory=directory,
        )

        doc = self._make_doc_without_judge(
            department="052",
            hearing_date=dt(2026, 2, 15),
        )
        result = scraper.parse_document(doc)

        assert result.judge_name == "Judge Alpha"
