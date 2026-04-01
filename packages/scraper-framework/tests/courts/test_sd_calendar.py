"""Tests for San Diego County civil calendar scraper (Phase 1).

Fixtures are realistic HTML pages based on the real calendar structure
captured from https://www.sandiego.courts.ca.gov/portal/online/calendar/.

Fixture files:
  sd_calendar_central.html — Central Division with multiple departments and
      motion-type events (Motion Hearing, Demurrer/Motion to Strike,
      Summary Judgment, Discovery Hearing, Motion to Quash, Motion for
      Sanctions, Class Action Certification) plus non-motion events
  sd_calendar_north.html   — North County Division with 1 motion + 1 CMC
  sd_calendar_empty.html   — Empty calendar page (no departments/hearings)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.sd_calendar import (
    CALENDAR_BASE_URL,
    SDCalendarScraper,
    _clean_case_title,
    _is_motion_event,
    _parse_calendar_date,
    _parse_judge_name,
    default_config,
    extract_case_section,
    parse_calendar_page,
)
from framework.models import ContentFormat, ScraperConfig

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _make_config() -> ScraperConfig:
    return ScraperConfig(
        scraper_id="ca-sd-calendar-test",
        state="CA",
        county="San Diego",
        court="Superior Court",
        target_urls=[CALENDAR_BASE_URL],
        request_delay_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# parse_calendar_page — against central fixture
# ---------------------------------------------------------------------------


class TestParseCalendarPage:
    """Tests for parsing a full calendar page HTML."""

    def test_parses_all_hearings(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        # 3 departments: C-60 (4 hearings), C-65 (3 hearings), C-67 (3 hearings)
        assert len(hearings) == 10

    def test_hearing_date_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        assert all(h.hearing_date is not None for h in hearings)
        assert hearings[0].hearing_date == datetime(2026, 3, 13)

    def test_division_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        assert all(h.division == "Central Division" for h in hearings)

    def test_courthouse_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        assert hearings[0].courthouse == "Central Courthouse"

    def test_departments_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        departments = {h.department for h in hearings}
        assert departments == {"C-60", "C-65", "C-67"}

    def test_case_numbers_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        case_numbers = [h.case_number for h in hearings]
        assert "24CU016153C" in case_numbers
        assert "25CU003887C" in case_numbers
        assert "23CU005421C" in case_numbers

    def test_event_types_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        events = {h.event_type for h in hearings}
        assert "Motion Hearing" in events
        assert "Demurrer/Motion to Strike" in events
        assert "Summary Judgment/Summary Adjudication" in events
        assert "Case Management Conference" in events

    def test_judge_names_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        # C-60 hearings have "Judge MATTHEW C. BRANER" → "Matthew C. Braner"
        c60 = [h for h in hearings if h.department == "C-60"]
        assert all(h.judge_name == "Matthew C. Braner" for h in c60)

    def test_generic_judge_names_filtered(self) -> None:
        """Generic department-based judge names like 'Judge C-67 Central' are None."""
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        c67 = [h for h in hearings if h.department == "C-67"]
        assert all(h.judge_name is None for h in c67)

    def test_case_titles_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        first = hearings[0]
        assert first.case_title == "Aasi et al vs American Honda Motor Co Inc"

    def test_imaged_suffix_stripped(self) -> None:
        """[IMAGED] suffix is removed from case titles."""
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        discovery = [h for h in hearings if h.event_type == "Discovery Hearing"][0]
        assert "[IMAGED]" not in discovery.case_title
        assert discovery.case_title == "Thompson vs Johnson"

    def test_parties_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        first = hearings[0]
        assert len(first.parties) == 3  # 2 PL + 1 DF (deduplicated)
        roles = {p["role"] for p in first.parties}
        assert "plaintiff" in roles
        assert "defendant" in roles

    def test_party_names_correct(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        first = hearings[0]
        names = {p["name"] for p in first.parties}
        assert "Sumayya Aasi" in names
        assert "Mohammad Aasi" in names
        assert "American Honda Motor Co Inc" in names

    def test_attorneys_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        first = hearings[0]
        assert len(first.attorneys) == 3
        assert "Robert M. Moss" in first.attorneys
        assert "Katherine E. Weber" in first.attorneys

    def test_hearing_times_extracted(self) -> None:
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        times = [h.hearing_time for h in hearings]
        assert "9:00 AM" in times
        assert "10:30 AM" in times

    def test_empty_calendar_returns_empty(self) -> None:
        html = _load_html("sd_calendar_empty.html")
        hearings = parse_calendar_page(html)
        assert hearings == []


class TestParseCalendarNorth:
    """Tests against the North County fixture."""

    def test_parses_north_county(self) -> None:
        html = _load_html("sd_calendar_north.html")
        hearings = parse_calendar_page(html)
        assert len(hearings) == 2

    def test_north_division(self) -> None:
        html = _load_html("sd_calendar_north.html")
        hearings = parse_calendar_page(html)
        assert all(h.division == "North County Division" for h in hearings)

    def test_north_courthouse_is_none(self) -> None:
        """North County h3 has no courthouse suffix, just 'NORTH COUNTY DIVISION'."""
        html = _load_html("sd_calendar_north.html")
        hearings = parse_calendar_page(html)
        assert all(h.courthouse is None for h in hearings)

    def test_north_department(self) -> None:
        html = _load_html("sd_calendar_north.html")
        hearings = parse_calendar_page(html)
        assert all(h.department == "N-28" for h in hearings)

    def test_north_judge_name(self) -> None:
        html = _load_html("sd_calendar_north.html")
        hearings = parse_calendar_page(html)
        assert hearings[0].judge_name == "Timothy R. Walsh"


# ---------------------------------------------------------------------------
# _is_motion_event — filtering logic
# ---------------------------------------------------------------------------


class TestMotionEventFiltering:
    """Tests for motion-type event filtering."""

    def test_motion_hearing_is_motion(self) -> None:
        assert _is_motion_event("Motion Hearing") is True

    def test_demurrer_is_motion(self) -> None:
        assert _is_motion_event("Demurrer/Motion to Strike") is True

    def test_summary_judgment_is_motion(self) -> None:
        assert _is_motion_event("Summary Judgment/Summary Adjudication") is True

    def test_discovery_hearing_is_motion(self) -> None:
        assert _is_motion_event("Discovery Hearing") is True

    def test_motion_to_quash_is_motion(self) -> None:
        assert _is_motion_event("Motion to Quash") is True

    def test_motion_for_sanctions_is_motion(self) -> None:
        assert _is_motion_event("Motion for Sanctions") is True

    def test_class_action_is_motion(self) -> None:
        assert _is_motion_event("Motion Hearing to Certify/Decertify Class Action") is True

    def test_case_insensitive(self) -> None:
        assert _is_motion_event("motion hearing") is True
        assert _is_motion_event("MOTION HEARING") is True

    def test_cmc_is_not_motion(self) -> None:
        assert _is_motion_event("Case Management Conference") is False

    def test_ex_parte_is_not_motion(self) -> None:
        assert _is_motion_event("Ex Parte Hearing") is False

    def test_restraining_order_is_not_motion(self) -> None:
        assert _is_motion_event("Hearing on Restraining Order") is False

    def test_name_change_is_not_motion(self) -> None:
        assert _is_motion_event("Hearing on Name Change") is False

    def test_central_fixture_motion_count(self) -> None:
        """Central fixture has 7 motion-type hearings and 3 non-motion."""
        html = _load_html("sd_calendar_central.html")
        hearings = parse_calendar_page(html)
        motion = [h for h in hearings if _is_motion_event(h.event_type)]
        non_motion = [h for h in hearings if not _is_motion_event(h.event_type)]
        assert len(motion) == 7
        assert len(non_motion) == 3


# ---------------------------------------------------------------------------
# _parse_judge_name — unit tests
# ---------------------------------------------------------------------------


class TestParseJudgeName:
    """Tests for judge name parsing."""

    def test_uppercase_name(self) -> None:
        assert _parse_judge_name("Judge MATTHEW C. BRANER") == "Matthew C. Braner"

    def test_mixed_case_name(self) -> None:
        assert _parse_judge_name("Judge Karen S. Hewitt") == "Karen S. Hewitt"

    def test_generic_department_name(self) -> None:
        assert _parse_judge_name("Judge 2102 Central") is None

    def test_generic_dept_code_name(self) -> None:
        assert _parse_judge_name("Judge C-60 Central") is None

    def test_generic_dept_with_letter(self) -> None:
        assert _parse_judge_name("Judge S-02 South") is None

    def test_empty_string(self) -> None:
        assert _parse_judge_name("") is None

    def test_whitespace_only(self) -> None:
        assert _parse_judge_name("   ") is None


# ---------------------------------------------------------------------------
# _parse_calendar_date — unit tests
# ---------------------------------------------------------------------------


class TestParseCalendarDate:
    """Tests for calendar date extraction from h1 header."""

    def test_standard_format(self) -> None:
        html = "<h1>CIVIL CALENDAR For Friday, 03/13/2026</h1>"
        assert _parse_calendar_date(html) == datetime(2026, 3, 13)

    def test_different_day(self) -> None:
        html = "<h1>CIVIL CALENDAR For Wednesday, 03/11/2026</h1>"
        assert _parse_calendar_date(html) == datetime(2026, 3, 11)

    def test_no_match(self) -> None:
        html = "<h1>Some Other Page</h1>"
        assert _parse_calendar_date(html) is None


# ---------------------------------------------------------------------------
# _clean_case_title — unit tests
# ---------------------------------------------------------------------------


class TestCleanCaseTitle:
    """Tests for case title cleanup."""

    def test_strips_imaged_suffix(self) -> None:
        assert _clean_case_title("Smith vs Jones [IMAGED]") == "Smith vs Jones"

    def test_strips_imaged_case_insensitive(self) -> None:
        assert _clean_case_title("Smith vs Jones [imaged]") == "Smith vs Jones"

    def test_no_imaged_suffix(self) -> None:
        assert _clean_case_title("Smith vs Jones") == "Smith vs Jones"

    def test_normalizes_whitespace(self) -> None:
        assert _clean_case_title("  Smith  vs   Jones  ") == "Smith vs Jones"


# ---------------------------------------------------------------------------
# SDCalendarScraper — integration test with respx
# ---------------------------------------------------------------------------


class TestSDCalendarScraper:
    """Integration tests for the full scraper with mocked HTTP responses."""

    @respx.mock
    def test_fetches_and_filters_motion_hearings(self) -> None:
        config = _make_config()
        scraper = SDCalendarScraper(config, day_numbers=[1])

        central_html = _load_html("sd_calendar_central.html")
        north_html = _load_html("sd_calendar_north.html")
        empty_html = _load_html("sd_calendar_empty.html")

        # Mock all 4 division URLs for day 1
        respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(
            return_value=httpx.Response(200, text=central_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL1.html").mock(
            return_value=httpx.Response(200, text=north_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )

        docs = scraper.fetch_documents()

        # Central: 7 motion events, North: 1 motion event
        assert len(docs) == 8

    @respx.mock
    def test_document_fields_populated(self) -> None:
        config = _make_config()
        scraper = SDCalendarScraper(config, day_numbers=[1])

        central_html = _load_html("sd_calendar_central.html")
        empty_html = _load_html("sd_calendar_empty.html")

        respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(
            return_value=httpx.Response(200, text=central_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )

        docs = scraper.fetch_documents()
        assert len(docs) > 0

        # Check first document (Motion Hearing in C-60)
        doc = docs[0]
        assert doc.case_number == "24CU016153C"
        assert doc.case_title == "Aasi et al vs American Honda Motor Co Inc"
        assert doc.department == "C-60"
        assert doc.courthouse == "Central Courthouse"
        assert doc.judge_name == "Matthew C. Braner"
        assert doc.hearing_date == datetime(2026, 3, 13)
        assert doc.motion_type == "Motion Hearing"
        assert doc.state == "CA"
        assert doc.county == "San Diego"
        assert len(doc.parties) == 3
        assert doc.extra["division"] == "Central Division"
        assert doc.extra["hearing_time"] == "9:00 AM"
        assert doc.extra["event_type"] == "Motion Hearing"
        assert "Robert M. Moss" in doc.extra["attorneys"]

    @respx.mock
    def test_non_motion_events_excluded(self) -> None:
        config = _make_config()
        scraper = SDCalendarScraper(config, day_numbers=[1])

        central_html = _load_html("sd_calendar_central.html")
        empty_html = _load_html("sd_calendar_empty.html")

        respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(
            return_value=httpx.Response(200, text=central_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )

        docs = scraper.fetch_documents()

        # Central has 3 non-motion events (1 CMC, 1 Restraining Order, 1 Ex Parte)
        # These should be excluded
        event_types = {doc.extra["event_type"] for doc in docs}
        assert "Case Management Conference" not in event_types
        assert "Hearing on Restraining Order" not in event_types
        assert "Ex Parte Hearing" not in event_types

    @respx.mock
    def test_handles_http_error_gracefully(self) -> None:
        config = _make_config()
        scraper = SDCalendarScraper(config, day_numbers=[1])

        empty_html = _load_html("sd_calendar_empty.html")

        # Central returns 500
        respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(return_value=httpx.Response(500))
        # Other divisions return empty
        respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )

        # Should not raise — logs error and continues
        docs = scraper.fetch_documents()
        assert docs == []

    @respx.mock
    def test_multiple_day_numbers(self) -> None:
        config = _make_config()
        scraper = SDCalendarScraper(config, day_numbers=[1, 2])

        central_html = _load_html("sd_calendar_central.html")
        empty_html = _load_html("sd_calendar_empty.html")

        # Day 1 and Day 2 for all 4 divisions = 8 requests
        for day in [1, 2]:
            respx.get(f"{CALENDAR_BASE_URL}/f_svcal{day}.html").mock(
                return_value=httpx.Response(200, text=central_html)
            )
            respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL{day}.html").mock(
                return_value=httpx.Response(200, text=empty_html)
            )
            respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL{day}.html").mock(
                return_value=httpx.Response(200, text=empty_html)
            )
            respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL{day}.html").mock(
                return_value=httpx.Response(200, text=empty_html)
            )

        docs = scraper.fetch_documents()
        # Central has 7 motion events, fetched twice = 14
        assert len(docs) == 14

    def test_parse_document_passthrough_with_ruling_text(self) -> None:
        """parse_document should not overwrite existing ruling_text."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        doc = CapturedDocument(
            scraper_id="test",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="http://example.com",
            capture_timestamp=datetime(2026, 3, 13),
            content_format=ContentFormat.HTML,
            raw_content=b"<html></html>",
            content_hash="abc123",
            case_number="24CU016153C",
            ruling_text="existing ruling text",
        )
        result = scraper.parse_document(doc)
        assert result.ruling_text == "existing ruling text"

    def test_parse_document_passthrough_no_case_number(self) -> None:
        """parse_document should return doc unchanged when case_number is None."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        doc = CapturedDocument(
            scraper_id="test",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="http://example.com",
            capture_timestamp=datetime(2026, 3, 13),
            content_format=ContentFormat.HTML,
            raw_content=b"<html></html>",
            content_hash="abc123",
        )
        result = scraper.parse_document(doc)
        assert result.ruling_text is None

    def test_parse_document_handles_decode_error(self) -> None:
        """parse_document handles non-UTF-8 raw_content gracefully."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        # Invalid UTF-8 bytes that will cause a decode error
        doc = CapturedDocument(
            scraper_id="test",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="http://example.com",
            capture_timestamp=datetime(2026, 3, 13),
            content_format=ContentFormat.HTML,
            raw_content=b"\xff\xfe",
            content_hash="abc123",
            case_number="24CU016153C",
        )
        # Should not raise — logs warning and returns doc unchanged
        result = scraper.parse_document(doc)
        assert result.ruling_text is None

    def test_parse_document_extracts_case_section(self) -> None:
        """parse_document narrows ruling_text to the specific case (#2311)."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        html = _load_html("sd_calendar_central.html")
        doc = CapturedDocument(
            scraper_id="test",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="http://example.com",
            capture_timestamp=datetime(2026, 3, 13),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="abc123",
            case_number="24CU016153C",
        )
        result = scraper.parse_document(doc)
        assert result.ruling_text is not None
        assert "24CU016153C" in result.ruling_text
        assert "Department: C-60" in result.ruling_text
        # Should NOT contain other cases from the same page
        assert "25CU003887C" not in result.ruling_text
        assert "23CU005421C" not in result.ruling_text

    @respx.mock
    def test_run_returns_health_event(self) -> None:
        """The run() method should return a ScraperHealthEvent."""
        config = _make_config()
        scraper = SDCalendarScraper(config, day_numbers=[1])

        central_html = _load_html("sd_calendar_central.html")
        empty_html = _load_html("sd_calendar_empty.html")

        respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(
            return_value=httpx.Response(200, text=central_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )
        respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL1.html").mock(
            return_value=httpx.Response(200, text=empty_html)
        )

        health = scraper.run()
        assert health.success is True
        assert health.records_captured == 7
        assert health.scraper_id == "ca-sd-calendar-test"
        assert health.response_time_seconds > 0


# ---------------------------------------------------------------------------
# extract_case_section — unit tests (#2311)
# ---------------------------------------------------------------------------


class TestExtractCaseSection:
    """Tests for extracting a single case section from calendar HTML."""

    def test_extracts_first_case_from_central(self) -> None:
        html = _load_html("sd_calendar_central.html")
        section = extract_case_section(html, "24CU016153C")
        assert section is not None
        assert "24CU016153C" in section
        assert "Department: C-60" in section
        assert "Motion Hearing" in section
        assert "Aasi et al vs American Honda Motor Co Inc" in section

    def test_extracts_judge_name(self) -> None:
        html = _load_html("sd_calendar_central.html")
        section = extract_case_section(html, "24CU016153C")
        assert section is not None
        assert "Judge MATTHEW C. BRANER" in section

    def test_extracts_parties(self) -> None:
        html = _load_html("sd_calendar_central.html")
        section = extract_case_section(html, "24CU016153C")
        assert section is not None
        assert "(PL) Sumayya Aasi" in section
        assert "(DF) - American Honda Motor Co Inc" in section

    def test_extracts_attorneys(self) -> None:
        html = _load_html("sd_calendar_central.html")
        section = extract_case_section(html, "24CU016153C")
        assert section is not None
        assert "Robert M. Moss" in section

    def test_includes_hearing_date(self) -> None:
        html = _load_html("sd_calendar_central.html")
        section = extract_case_section(html, "24CU016153C")
        assert section is not None
        assert "03/13/2026" in section

    def test_extracts_case_from_different_department(self) -> None:
        """Should find a case in C-65 department."""
        html = _load_html("sd_calendar_central.html")
        section = extract_case_section(html, "25CU008912C")
        assert section is not None
        assert "Department: C-65" in section
        assert "Discovery Hearing" in section
        assert "Thompson" in section

    def test_does_not_include_other_cases(self) -> None:
        """Extracted section should only contain the requested case."""
        html = _load_html("sd_calendar_central.html")
        section = extract_case_section(html, "24CU016153C")
        assert section is not None
        # Other case numbers from the same page should NOT appear
        assert "25CU003887C" not in section
        assert "23CU005421C" not in section
        assert "26CL011234C" not in section

    def test_returns_none_for_missing_case(self) -> None:
        html = _load_html("sd_calendar_central.html")
        section = extract_case_section(html, "NONEXISTENT123")
        assert section is None

    def test_returns_none_for_empty_html(self) -> None:
        html = _load_html("sd_calendar_empty.html")
        section = extract_case_section(html, "24CU016153C")
        assert section is None

    def test_section_is_small(self) -> None:
        """Extracted section should be much smaller than the full page."""
        html = _load_html("sd_calendar_central.html")
        section = extract_case_section(html, "24CU016153C")
        assert section is not None
        # The full page is ~7KB; the section should be well under 1KB
        assert len(section) < 1000
        assert len(section) < len(html) / 3

    def test_handles_dept_without_h2(self) -> None:
        """Department div without h2 is skipped gracefully."""
        html = '<div class="department"><table class="tables"><tbody><tr><td>x</td></tr></tbody></table></div>'
        assert extract_case_section(html, "ANY") is None

    def test_handles_dept_without_table(self) -> None:
        """Department div without table is skipped gracefully."""
        html = '<div class="department"><h2>Department: C-60</h2></div>'
        assert extract_case_section(html, "ANY") is None

    def test_handles_dept_without_tbody(self) -> None:
        """Department div without tbody is skipped gracefully."""
        html = '<div class="department"><h2>Department: C-60</h2><table class="tables"></table></div>'
        assert extract_case_section(html, "ANY") is None

    def test_handles_row_with_few_columns(self) -> None:
        """Rows with fewer than 7 columns are skipped."""
        html = (
            '<div class="department"><h2>Department: C-60</h2>'
            '<table class="tables"><tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>'
        )
        assert extract_case_section(html, "ANY") is None


# ---------------------------------------------------------------------------
# default_config — factory test
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    """Tests for the default configuration factory."""

    def test_scraper_id(self) -> None:
        config = default_config()
        assert config.scraper_id == "ca-sd-calendar"

    def test_state_and_county(self) -> None:
        config = default_config()
        assert config.state == "CA"
        assert config.county == "San Diego"

    def test_schedule_windows(self) -> None:
        config = default_config()
        assert len(config.schedule_windows) == 2
        # Primary window: 4:30 PM
        assert config.schedule_windows[0].start.hour == 16
        assert config.schedule_windows[0].start.minute == 30

    def test_s3_bucket_parameter(self) -> None:
        config = default_config(s3_bucket="my-bucket")
        assert config.s3_bucket == "my-bucket"

    def test_poll_interval(self) -> None:
        config = default_config()
        assert config.poll_interval_seconds == 86400  # daily
