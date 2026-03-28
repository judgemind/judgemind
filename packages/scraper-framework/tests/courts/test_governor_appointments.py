"""Tests for the Governor's judicial appointment press release scraper.

Tests use mocked HTTP responses and archived fixture HTML to verify:
- Search result discovery and pagination
- Press release parsing (per-judge biographical data extraction)
- Field extraction: name, county, court, position, education
- Edge cases: varied name formats, missing fields, pagination end
- BaseScraper integration (run loop, health reporting)
- Data model correctness (judge_bio_sources format)

Issue: #2165
"""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from courts.ca.governor_appointments import (
    SEARCH_URL,
    Appointee,
    GovernorAppointmentsScraper,
    _is_appointment_url,
    default_config,
    extract_next_page_url,
    extract_search_results,
    parse_appointees,
)
from framework import ContentFormat, ScraperConfig

# ---------------------------------------------------------------------------
# Fixtures — load archived HTML files
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "governor_appointments"


def _load_fixture(filename: str) -> str:
    """Load an HTML fixture file as a string."""
    return (_FIXTURES_DIR / filename).read_text(encoding="utf-8")


def _make_scraper_config() -> ScraperConfig:
    """Build a minimal scraper config for testing."""
    return ScraperConfig(
        scraper_id="ca-governor-appointments-test",
        state="CA",
        county="Statewide",
        court="Governor",
        target_urls=[f"{SEARCH_URL}?s=judicial+appointment"],
        request_delay_seconds=0,
        request_timeout_seconds=10.0,
        max_retries=1,
    )


# ---------------------------------------------------------------------------
# Search result parsing tests
# ---------------------------------------------------------------------------


class TestExtractSearchResults:
    """Tests for extracting press release URLs from search results."""

    def test_extracts_appointment_links(self) -> None:
        """Search results page yields judicial appointment press release URLs."""
        html = _load_fixture("search_results_page1.html")
        results = extract_search_results(html, SEARCH_URL)
        assert len(results) == 10
        # All should be appointment press releases
        for r in results:
            assert "judicial-appointment" in r["url"].lower()
            assert "gov.ca.gov" in r["url"]

    def test_filters_non_appointment_links(self) -> None:
        """Non-appointment links (infrastructure bill, etc.) are filtered out."""
        html = _load_fixture("search_results_page1.html")
        results = extract_search_results(html, SEARCH_URL)
        urls = [r["url"] for r in results]
        assert all("infrastructure" not in u for u in urls)

    def test_extracts_dates_from_urls(self) -> None:
        """Press release dates are parsed from URL path."""
        html = _load_fixture("search_results_page1.html")
        results = extract_search_results(html, SEARCH_URL)
        # First result is dated 2026-03-15
        assert results[0]["date"] == "2026-03-15"
        # Second result is dated 2026-02-10
        assert results[1]["date"] == "2026-02-10"

    def test_extracts_titles(self) -> None:
        """Press release titles are captured."""
        html = _load_fixture("search_results_page1.html")
        results = extract_search_results(html, SEARCH_URL)
        assert "Judicial Appointments 3.15.26" in results[0]["title"]

    def test_deduplicates_urls(self) -> None:
        """Duplicate URLs in search results are removed."""
        html = """
        <html><body>
        <a href="https://www.gov.ca.gov/2026/01/01/governor-newsom-announces-judicial-appointments-1/">
          Governor Newsom Announces Judicial Appointments</a>
        <a href="https://www.gov.ca.gov/2026/01/01/governor-newsom-announces-judicial-appointments-1/">
          Governor Newsom Announces Judicial Appointments (duplicate)</a>
        </body></html>
        """
        results = extract_search_results(html, SEARCH_URL)
        assert len(results) == 1

    def test_page2_results(self) -> None:
        """Second page of search results is parsed correctly."""
        html = _load_fixture("search_results_page2.html")
        results = extract_search_results(html, SEARCH_URL)
        assert len(results) == 3


class TestExtractNextPageUrl:
    """Tests for pagination link extraction."""

    def test_finds_next_page_link(self) -> None:
        """Next page link with class='next' is found."""
        html = _load_fixture("search_results_page1.html")
        next_url = extract_next_page_url(html, SEARCH_URL)
        assert next_url is not None
        assert "page/2" in next_url

    def test_no_next_page_on_last_page(self) -> None:
        """No next page link returns None on last page."""
        html = _load_fixture("search_results_page2.html")
        next_url = extract_next_page_url(html, SEARCH_URL)
        assert next_url is None

    def test_finds_next_with_text(self) -> None:
        """Next page link with 'Next' text is found."""
        html = """
        <html><body>
        <div class="pagination">
            <a href="/page/3/?s=test">Next</a>
        </div>
        </body></html>
        """
        next_url = extract_next_page_url(html, "https://www.gov.ca.gov/page/2/")
        assert next_url is not None
        assert "page/3" in next_url

    def test_finds_next_with_rel_next(self) -> None:
        """Next page link with rel='next' is found."""
        html = """
        <html><body>
        <a rel="next" href="/page/2/?s=test">2</a>
        </body></html>
        """
        next_url = extract_next_page_url(html, "https://www.gov.ca.gov/")
        assert next_url is not None
        assert "page/2" in next_url


class TestIsAppointmentUrl:
    """Tests for the URL filter function."""

    def test_appointment_url(self) -> None:
        """Standard appointment URL is recognized."""
        assert _is_appointment_url(
            "https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments/",
            "Governor Newsom Announces Judicial Appointments",
        )

    def test_non_gov_url_rejected(self) -> None:
        """Non-gov.ca.gov URLs are rejected."""
        assert not _is_appointment_url(
            "https://example.com/judicial-appointments/",
            "Judicial Appointments",
        )

    def test_unrelated_url_rejected(self) -> None:
        """Unrelated gov.ca.gov URLs are rejected."""
        assert not _is_appointment_url(
            "https://www.gov.ca.gov/2026/03/10/governor-signs-infrastructure-bill/",
            "Governor Signs Infrastructure Bill",
        )

    def test_title_based_matching(self) -> None:
        """Title-based matching catches appointment press releases."""
        assert _is_appointment_url(
            "https://www.gov.ca.gov/2026/03/15/some-url/",
            "Governor Newsom Announces Judicial Appointments",
        )


# ---------------------------------------------------------------------------
# Press release parsing tests
# ---------------------------------------------------------------------------


class TestParseAppointees:
    """Tests for extracting per-judge biographical data from press releases."""

    def test_extracts_all_appointees(self) -> None:
        """All appointees in the sample press release are found."""
        html = _load_fixture("press_release_sample.html")
        appointees = parse_appointees(html)
        assert len(appointees) == 4

    def test_extracts_names(self) -> None:
        """Full names are correctly extracted."""
        html = _load_fixture("press_release_sample.html")
        appointees = parse_appointees(html)
        names = [a.name for a in appointees]
        assert "Maria Elena Rodriguez" in names
        assert "James Michael Chen" in names
        assert "Aisha Nicole Williams" in names
        assert "Robert David Kim" in names

    def test_extracts_counties(self) -> None:
        """Counties are correctly extracted."""
        html = _load_fixture("press_release_sample.html")
        appointees = parse_appointees(html)
        counties = {a.name: a.county for a in appointees}
        assert counties["Maria Elena Rodriguez"] == "Los Angeles"
        assert counties["James Michael Chen"] == "Orange"
        assert counties["Aisha Nicole Williams"] == "San Diego"
        assert counties["Robert David Kim"] == "Sacramento"

    def test_extracts_courts(self) -> None:
        """Court assignments are correctly extracted."""
        html = _load_fixture("press_release_sample.html")
        appointees = parse_appointees(html)
        courts = {a.name: a.court for a in appointees}
        assert "Los Angeles County Superior Court" in courts["Maria Elena Rodriguez"]
        assert "Orange County Superior Court" in courts["James Michael Chen"]

    def test_extracts_current_position(self) -> None:
        """Current position and employer are extracted."""
        html = _load_fixture("press_release_sample.html")
        appointees = parse_appointees(html)
        by_name = {a.name: a for a in appointees}

        rodriguez = by_name["Maria Elena Rodriguez"]
        assert rodriguez.current_position == "Deputy District Attorney"
        assert "Los Angeles County District Attorney" in (rodriguez.employer or "")
        assert rodriguez.since_year == 2015

    def test_extracts_education(self) -> None:
        """Education credentials are extracted (both JD and undergraduate)."""
        html = _load_fixture("press_release_sample.html")
        appointees = parse_appointees(html)
        by_name = {a.name: a for a in appointees}

        rodriguez = by_name["Maria Elena Rodriguez"]
        assert len(rodriguez.education) == 2
        degrees = [e["degree"] for e in rodriguez.education]
        institutions = [e["institution"] for e in rodriguez.education]
        assert any("Juris Doctor" in d for d in degrees)
        assert any("UCLA" in i for i in institutions)
        assert any("Bachelor of Arts" in d for d in degrees)
        assert any("University of California" in i for i in institutions)

    def test_extracts_previous_positions(self) -> None:
        """Previous positions are extracted when present."""
        html = _load_fixture("press_release_sample.html")
        appointees = parse_appointees(html)
        by_name = {a.name: a for a in appointees}

        williams = by_name["Aisha Nicole Williams"]
        assert len(williams.previous_positions) >= 1
        prev_employers = [p["employer"] for p in williams.previous_positions]
        assert any("Latham" in e for e in prev_employers)

    def test_varied_name_formats(self) -> None:
        """Handles varied name formats (hyphenated, apostrophes)."""
        html = _load_fixture("press_release_varied.html")
        appointees = parse_appointees(html)
        names = [a.name for a in appointees]
        assert any("O'Brien" in n for n in names)
        assert any("Martinez-Garcia" in n for n in names)

    def test_to_dict_format(self) -> None:
        """Appointee.to_dict() produces correct judge_bio_sources format."""
        html = _load_fixture("press_release_sample.html")
        appointees = parse_appointees(html)
        data = appointees[0].to_dict()

        # Required fields per issue spec
        assert "name" in data
        assert "county" in data
        assert "court" in data
        assert "raw_text" in data
        assert isinstance(data["name"], str)
        assert len(data["name"]) > 0

    def test_appointee_dict_has_source_fields(self) -> None:
        """Appointee dict includes all expected fields for judge_bio_sources."""
        html = _load_fixture("press_release_sample.html")
        appointees = parse_appointees(html)
        data = appointees[0].to_dict()

        # Core identification
        assert "name" in data
        assert "county" in data
        assert "court" in data
        assert "role" in data

        # Position data (present for first appointee)
        assert "current_position" in data
        assert "employer" in data

        # Education (present for first appointee)
        assert "education" in data
        assert len(data["education"]) > 0

    def test_multi_paragraph_appointee(self) -> None:
        """Bio data split across multiple paragraphs is accumulated."""
        html = _load_fixture("press_release_multi_paragraph.html")
        appointees = parse_appointees(html)

        assert len(appointees) == 2

        by_name = {a.name: a for a in appointees}
        thompson = by_name["Sandra Lee Thompson"]
        assert thompson.county == "Alameda"

        # Education is in a separate paragraph — must be accumulated.
        # Both JD and BA should be extracted.
        assert len(thompson.education) == 2
        institutions = [e["institution"] for e in thompson.education]
        assert any("Boalt" in i for i in institutions)
        assert any("Stanford" in i for i in institutions)

        # Previous positions are in a third paragraph.
        assert len(thompson.previous_positions) >= 1
        prev_employers = [p["employer"] for p in thompson.previous_positions]
        assert any("Pillsbury" in e for e in prev_employers)

    def test_multi_paragraph_raw_text_accumulated(self) -> None:
        """raw_text includes text from all accumulated paragraphs."""
        html = _load_fixture("press_release_multi_paragraph.html")
        appointees = parse_appointees(html)

        by_name = {a.name: a for a in appointees}
        thompson = by_name["Sandra Lee Thompson"]

        # raw_text should contain content from all three paragraphs
        assert "Alameda County Superior Court" in thompson.raw_text
        assert "Boalt Hall" in thompson.raw_text
        assert "Pillsbury" in thompson.raw_text

    def test_semicolon_terminated_appointment(self) -> None:
        """Appointment sentence terminated by semicolon is parsed."""
        html = _load_fixture("press_release_multi_paragraph.html")
        appointees = parse_appointees(html)

        by_name = {a.name: a for a in appointees}
        oconnor = by_name["Michael James O'Connor"]
        assert oconnor.county == "Ventura"
        assert "Ventura County Superior Court" in oconnor.court

    def test_semicolon_terminated_inline(self) -> None:
        """Appointment pattern ending with semicolon in single paragraph."""
        html = """
        <html><body>
        <p>Sarah Jane Miller, of Kern, has been appointed to serve as a
        Judge in the Kern County Superior Court; she replaces Judge Smith.
        Miller has served as a Deputy Public Defender at the Kern County
        Public Defender since 2016.</p>
        </body></html>
        """
        appointees = parse_appointees(html)
        assert len(appointees) == 1
        assert appointees[0].name == "Sarah Jane Miller"
        assert appointees[0].county == "Kern"
        assert "Kern County Superior Court" in appointees[0].court

    def test_no_appointees_in_non_appointment_text(self) -> None:
        """Non-appointment HTML returns empty list."""
        html = (
            "<html><body><p>This is a regular press release about infrastructure.</p></body></html>"
        )
        appointees = parse_appointees(html)
        assert len(appointees) == 0

    def test_empty_html(self) -> None:
        """Empty HTML returns empty list."""
        appointees = parse_appointees("")
        assert len(appointees) == 0


# ---------------------------------------------------------------------------
# GovernorAppointmentsScraper integration tests
# ---------------------------------------------------------------------------


class TestGovernorAppointmentsScraper:
    """Tests for the full scraper integration."""

    @respx.mock
    def test_fetch_documents_discovers_and_fetches(self) -> None:
        """Scraper discovers press releases from search and fetches each one."""
        press_release_html = _load_fixture("press_release_sample.html")

        # Mock search - single result for simplicity
        minimal_search = """
        <html><body>
        <a href="https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/">
            Governor Newsom Announces Judicial Appointments 3.15.26</a>
        </body></html>
        """

        respx.get(f"{SEARCH_URL}?s=judicial+appointment").mock(
            return_value=httpx.Response(200, text=minimal_search)
        )
        respx.get(
            "https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/"
        ).mock(return_value=httpx.Response(200, text=press_release_html))

        config = _make_scraper_config()
        scraper = GovernorAppointmentsScraper(config, max_pages=1)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        doc = docs[0]
        assert doc.content_format == ContentFormat.HTML
        assert doc.state == "CA"
        assert doc.county == "Statewide"
        assert "governor-newsom-announces-judicial-appointments" in doc.source_url

    @respx.mock
    def test_parse_document_extracts_appointees(self) -> None:
        """parse_document() populates extra with per-judge data."""
        press_release_html = _load_fixture("press_release_sample.html")

        minimal_search = """
        <html><body>
        <a href="https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/">
            Governor Newsom Announces Judicial Appointments 3.15.26</a>
        </body></html>
        """

        respx.get(f"{SEARCH_URL}?s=judicial+appointment").mock(
            return_value=httpx.Response(200, text=minimal_search)
        )
        respx.get(
            "https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/"
        ).mock(return_value=httpx.Response(200, text=press_release_html))

        config = _make_scraper_config()
        scraper = GovernorAppointmentsScraper(config, max_pages=1)
        health = scraper.run()

        assert health.success is True
        assert health.records_captured == 1

    @respx.mock
    def test_paginated_search(self) -> None:
        """Scraper follows pagination links to discover more press releases."""
        page1 = _load_fixture("search_results_page1.html")
        page2 = _load_fixture("search_results_page2.html")
        press_release_html = _load_fixture("press_release_sample.html")

        respx.get(f"{SEARCH_URL}?s=judicial+appointment").mock(
            return_value=httpx.Response(200, text=page1)
        )
        respx.get("https://www.gov.ca.gov/page/2/?s=judicial+appointment").mock(
            return_value=httpx.Response(200, text=page2)
        )
        # Mock all press release fetches with the same content
        respx.get(url__startswith="https://www.gov.ca.gov/202").mock(
            return_value=httpx.Response(200, text=press_release_html)
        )

        config = _make_scraper_config()
        scraper = GovernorAppointmentsScraper(config, max_pages=2)
        docs = scraper.fetch_documents()

        # Page 1 has 10 results, page 2 has 3 = 13 total
        assert len(docs) == 13

    @respx.mock
    def test_handles_fetch_errors_gracefully(self) -> None:
        """Scraper continues when individual press release fetches fail."""
        minimal_search = """
        <html><body>
        <a href="https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-1/">
            Governor Newsom Announces Judicial Appointments 1</a>
        <a href="https://www.gov.ca.gov/2026/03/14/governor-newsom-announces-judicial-appointments-2/">
            Governor Newsom Announces Judicial Appointments 2</a>
        </body></html>
        """
        press_release_html = _load_fixture("press_release_sample.html")

        respx.get(f"{SEARCH_URL}?s=judicial+appointment").mock(
            return_value=httpx.Response(200, text=minimal_search)
        )
        # First press release fails
        respx.get(
            "https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-1/"
        ).mock(return_value=httpx.Response(500))
        # Second succeeds
        respx.get(
            "https://www.gov.ca.gov/2026/03/14/governor-newsom-announces-judicial-appointments-2/"
        ).mock(return_value=httpx.Response(200, text=press_release_html))

        config = _make_scraper_config()
        scraper = GovernorAppointmentsScraper(config, max_pages=1)
        docs = scraper.fetch_documents()

        assert len(docs) == 1

    @respx.mock
    def test_document_extra_has_appointee_data(self) -> None:
        """CapturedDocument.extra contains parsed appointee data."""
        press_release_html = _load_fixture("press_release_sample.html")

        minimal_search = """
        <html><body>
        <a href="https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/">
            Governor Newsom Announces Judicial Appointments 3.15.26</a>
        </body></html>
        """

        respx.get(f"{SEARCH_URL}?s=judicial+appointment").mock(
            return_value=httpx.Response(200, text=minimal_search)
        )
        respx.get(
            "https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/"
        ).mock(return_value=httpx.Response(200, text=press_release_html))

        config = _make_scraper_config()
        scraper = GovernorAppointmentsScraper(config, max_pages=1)
        docs = scraper.fetch_documents()
        assert len(docs) == 1

        # Run parse_document to populate extra
        doc = scraper.parse_document(docs[0])

        assert "appointees" in doc.extra
        assert doc.extra["appointee_count"] == 4
        appointees = doc.extra["appointees"]
        assert len(appointees) == 4

        # Verify structure of first appointee
        first = appointees[0]
        assert "name" in first
        assert "county" in first
        assert "court" in first
        assert "raw_text" in first

    @respx.mock
    def test_document_extra_has_source_type(self) -> None:
        """CapturedDocument.extra includes source_type for bio source identification."""
        press_release_html = _load_fixture("press_release_sample.html")

        minimal_search = """
        <html><body>
        <a href="https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/">
            Governor Newsom Announces Judicial Appointments 3.15.26</a>
        </body></html>
        """

        respx.get(f"{SEARCH_URL}?s=judicial+appointment").mock(
            return_value=httpx.Response(200, text=minimal_search)
        )
        respx.get(
            "https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/"
        ).mock(return_value=httpx.Response(200, text=press_release_html))

        config = _make_scraper_config()
        scraper = GovernorAppointmentsScraper(config, max_pages=1)
        docs = scraper.fetch_documents()

        assert docs[0].extra["source_type"] == "governor_appointment"
        assert "press_release_title" in docs[0].extra

    @respx.mock
    def test_hearing_date_from_url(self) -> None:
        """Press release date from URL is parsed into hearing_date."""
        press_release_html = _load_fixture("press_release_sample.html")

        minimal_search = """
        <html><body>
        <a href="https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/">
            Governor Newsom Announces Judicial Appointments 3.15.26</a>
        </body></html>
        """

        respx.get(f"{SEARCH_URL}?s=judicial+appointment").mock(
            return_value=httpx.Response(200, text=minimal_search)
        )
        respx.get(
            "https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/"
        ).mock(return_value=httpx.Response(200, text=press_release_html))

        config = _make_scraper_config()
        scraper = GovernorAppointmentsScraper(config, max_pages=1)
        docs = scraper.fetch_documents()

        assert docs[0].hearing_date is not None
        assert docs[0].hearing_date.year == 2026
        assert docs[0].hearing_date.month == 3
        assert docs[0].hearing_date.day == 15

    @respx.mock
    def test_run_returns_health_event(self) -> None:
        """Full run() produces a successful ScraperHealthEvent."""
        press_release_html = _load_fixture("press_release_sample.html")

        minimal_search = """
        <html><body>
        <a href="https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/">
            Governor Newsom Announces Judicial Appointments 3.15.26</a>
        </body></html>
        """

        respx.get(f"{SEARCH_URL}?s=judicial+appointment").mock(
            return_value=httpx.Response(200, text=minimal_search)
        )
        respx.get(
            "https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments-3-15-26/"
        ).mock(return_value=httpx.Response(200, text=press_release_html))

        config = _make_scraper_config()
        scraper = GovernorAppointmentsScraper(config, max_pages=1)
        health = scraper.run()

        assert health.success is True
        assert health.records_captured == 1
        assert health.scraper_id == "ca-governor-appointments-test"

    @respx.mock
    def test_max_pages_limit(self) -> None:
        """Scraper stops paginating at max_pages."""
        # Create infinite pagination
        page_html = """
        <html><body>
        <a href="https://www.gov.ca.gov/2026/03/15/governor-newsom-announces-judicial-appointments/">
            Governor Newsom Announces Judicial Appointments</a>
        <a class="next" href="https://www.gov.ca.gov/page/999/?s=judicial+appointment">Next</a>
        </body></html>
        """
        press_release_html = _load_fixture("press_release_sample.html")

        respx.get(url__startswith=SEARCH_URL).mock(return_value=httpx.Response(200, text=page_html))
        respx.get(url__startswith="https://www.gov.ca.gov/page/").mock(
            return_value=httpx.Response(200, text=page_html)
        )
        respx.get(url__startswith="https://www.gov.ca.gov/2026/").mock(
            return_value=httpx.Response(200, text=press_release_html)
        )

        config = _make_scraper_config()
        scraper = GovernorAppointmentsScraper(config, max_pages=2)
        docs = scraper.fetch_documents()

        # Should fetch at most 2 pages of search results (1 result per page = 2 results)
        # But dedup means only 1 unique URL, so 1 doc
        assert len(docs) <= 2


# ---------------------------------------------------------------------------
# Configuration tests
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    """Tests for the default configuration factory."""

    def test_default_config_values(self) -> None:
        """Default config has expected scraper ID and settings."""
        config = default_config()
        assert config.scraper_id == "ca-governor-appointments"
        assert config.state == "CA"
        assert config.county == "Statewide"
        assert config.court == "Governor"
        assert config.request_delay_seconds == 2.0
        assert config.poll_interval_seconds == 604800  # weekly

    def test_default_config_with_bucket(self) -> None:
        """Default config accepts S3 bucket parameter."""
        config = default_config(s3_bucket="my-bucket")
        assert config.s3_bucket == "my-bucket"

    def test_default_config_target_urls(self) -> None:
        """Default config points to the gov.ca.gov search URL."""
        config = default_config()
        assert len(config.target_urls) == 1
        assert "gov.ca.gov" in config.target_urls[0]

    def test_schedule_windows(self) -> None:
        """Default config has schedule windows for off-peak scraping."""
        config = default_config()
        assert len(config.schedule_windows) == 1
        assert config.schedule_windows[0].timezone == "America/Los_Angeles"


# ---------------------------------------------------------------------------
# Appointee data model tests
# ---------------------------------------------------------------------------


class TestAppointee:
    """Tests for the Appointee dataclass."""

    def test_to_dict_minimal(self) -> None:
        """to_dict with minimal fields."""
        a = Appointee(name="Jane Doe", county="Los Angeles", court="LA Superior Court")
        d = a.to_dict()
        assert d["name"] == "Jane Doe"
        assert d["county"] == "Los Angeles"
        assert d["court"] == "LA Superior Court"
        assert d["role"] == "Judge"
        assert "current_position" not in d
        assert "employer" not in d

    def test_to_dict_full(self) -> None:
        """to_dict with all fields populated."""
        a = Appointee(
            name="John Smith",
            county="Orange",
            court="OC Superior Court",
            role="Judge",
            current_position="Deputy DA",
            employer="OC DA Office",
            since_year=2015,
            education=[{"degree": "JD", "institution": "Stanford"}],
            previous_positions=[{"role": "Associate", "employer": "Jones Day"}],
            raw_text="John Smith, of Orange...",
        )
        d = a.to_dict()
        assert d["current_position"] == "Deputy DA"
        assert d["employer"] == "OC DA Office"
        assert d["since_year"] == 2015
        assert len(d["education"]) == 1
        assert len(d["previous_positions"]) == 1
        assert d["raw_text"] == "John Smith, of Orange..."
