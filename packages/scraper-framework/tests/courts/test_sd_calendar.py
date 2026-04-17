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
    SDSplitRuling,
    _case_numbers_match,
    _clean_case_title,
    _is_motion_event,
    _parse_calendar_date,
    _parse_judge_name,
    _split_rulings,
    default_config,
    extract_case_section,
    is_sd_calendar_html,
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
        html = (
            '<div class="department">'
            '<table class="tables"><tbody>'
            "<tr><td>x</td></tr>"
            "</tbody></table></div>"
        )
        assert extract_case_section(html, "ANY") is None

    def test_handles_dept_without_table(self) -> None:
        """Department div without table is skipped gracefully."""
        html = '<div class="department"><h2>Department: C-60</h2></div>'
        assert extract_case_section(html, "ANY") is None

    def test_handles_dept_without_tbody(self) -> None:
        """Department div without tbody is skipped gracefully."""
        html = (
            '<div class="department"><h2>Department: C-60</h2><table class="tables"></table></div>'
        )
        assert extract_case_section(html, "ANY") is None

    def test_handles_row_with_few_columns(self) -> None:
        """Rows with fewer than 7 columns are skipped."""
        html = (
            '<div class="department">'
            "<h2>Department: C-60</h2>"
            '<table class="tables"><tbody>'
            "<tr><td>1</td><td>2</td></tr>"
            "</tbody></table></div>"
        )
        assert extract_case_section(html, "ANY") is None


# ---------------------------------------------------------------------------
# _case_numbers_match — Odyssey short-form matching (#2381)
# ---------------------------------------------------------------------------


class TestCaseNumbersMatch:
    """Tests for matching stored case numbers against calendar row case numbers.

    San Diego documents created by ``rebuild_db.py`` can have Odyssey
    short-form case numbers (e.g. ``2024-00021082``) while the calendar
    HTML uses the full Odyssey form (``37-2024-00021082-CL-CL-CTL``).
    """

    def test_exact_match(self) -> None:
        assert _case_numbers_match("24CU016153C", "24CU016153C") is True

    def test_exact_match_with_whitespace(self) -> None:
        assert _case_numbers_match(" 24CU016153C ", "24CU016153C") is True

    def test_substring_match_anchored_by_dashes(self) -> None:
        assert _case_numbers_match("2024-00021082", "37-2024-00021082-CL-CL-CTL") is True

    def test_substring_match_at_start(self) -> None:
        assert _case_numbers_match("2024-00021082", "2024-00021082-CL") is True

    def test_substring_match_at_end(self) -> None:
        assert _case_numbers_match("2024-00021082", "37-2024-00021082") is True

    def test_no_boundary_before_substring(self) -> None:
        """Reject match where the stored value starts mid-digit-run."""
        # '24-00021082' is inside '37-2024-00021082-CL-CL-CTL' but the
        # character before ('0') is alphanumeric, so not a real match.
        assert _case_numbers_match("24-00021082", "37-2024-00021082-CL-CL-CTL") is False

    def test_no_boundary_after_substring(self) -> None:
        """Reject match where the stored value ends mid-digit-run."""
        # '2024-0002' is inside '37-2024-00021082-CL-CL-CTL' but '1' after
        # is alphanumeric, so not a real match.
        assert _case_numbers_match("2024-0002", "37-2024-00021082-CL-CL-CTL") is False

    def test_empty_stored(self) -> None:
        assert _case_numbers_match("", "37-2024-00021082-CL-CL-CTL") is False

    def test_empty_row(self) -> None:
        assert _case_numbers_match("2024-00021082", "") is False

    def test_both_empty(self) -> None:
        assert _case_numbers_match("", "") is False

    def test_none_safe(self) -> None:
        # Defensive: code paths may pass None — the helper should not raise.
        assert _case_numbers_match(None, "x") is False  # type: ignore[arg-type]
        assert _case_numbers_match("x", None) is False  # type: ignore[arg-type]

    def test_different_numbers_no_match(self) -> None:
        assert _case_numbers_match("24CU016153C", "24CU016154C") is False

    def test_substring_with_alpha_boundary_rejected(self) -> None:
        """A substring match must have non-alphanumeric boundaries.

        '016153' is inside '24CU016153C' but both sides are alphanumeric.
        """
        assert _case_numbers_match("016153", "24CU016153C") is False


# ---------------------------------------------------------------------------
# extract_case_section — Odyssey short-form substring matching (#2381)
# ---------------------------------------------------------------------------


class TestExtractCaseSectionOdyssey:
    """Tests that extract_case_section tolerates Odyssey short-form case numbers."""

    _BASE_HTML_TEMPLATE = """<html><head></head><body>
<h1>CIVIL CALENDAR For Friday, 03/21/2026</h1>
<h3>CENTRAL DIVISION, CENTRAL COURTHOUSE</h3>
<div class="department">
<h2><a name='C-60'></a>Department: C-60</h2>
<table class="tables">
<thead><tr><th>Time</th><th>Case#</th><th>Title</th><th>Event</th><th>Officer</th><th>Party</th><th>Attorney</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
</body></html>
"""

    @staticmethod
    def _make_row(
        case_number: str,
        case_title: str = "Smith v Jones",
        event_type: str = "Motion Hearing",
        judge: str = "Judge MATTHEW C. BRANER",
    ) -> str:
        return (
            "<tr>"
            "<td>9:00 AM</td>"
            f"<td>{case_number}</td>"
            f"<td>{case_title}</td>"
            f"<td>{event_type}</td>"
            f"<td>{judge}</td>"
            "<td><p>(PL) John Smith</p><p>(DF) Robert Jones</p></td>"
            "<td><p>Jane Doe</p></td>"
            "</tr>"
        )

    @classmethod
    def _build_html(cls, *rows: str) -> str:
        return cls._BASE_HTML_TEMPLATE.format(rows="\n".join(rows))

    def test_matches_odyssey_short_form_within_long_form(self) -> None:
        """Stored short-form ``2024-00021082`` matches row ``37-2024-00021082-CL-CL-CTL``."""
        html = self._build_html(self._make_row("37-2024-00021082-CL-CL-CTL"))
        section = extract_case_section(html, "2024-00021082")
        assert section is not None
        assert "Department: C-60" in section
        # Display the full row case number (more informative).
        assert "Case Number: 37-2024-00021082-CL-CL-CTL" in section
        assert "Motion Hearing" in section
        # Should NOT be raw HTML.
        assert not section.startswith("<")

    def test_prefers_exact_match_over_substring(self) -> None:
        """If both exact and substring matches exist, exact wins."""
        html = self._build_html(
            self._make_row(
                "37-2024-00021082-CL-CL-CTL",
                case_title="Long-form case",
            ),
            self._make_row("2024-00021082", case_title="Exact match case"),
        )
        section = extract_case_section(html, "2024-00021082")
        assert section is not None
        assert "Case Number: 2024-00021082" in section
        assert "Exact match case" in section
        assert "Long-form case" not in section

    def test_no_substring_match_when_boundary_invalid(self) -> None:
        """``24-00021082`` should NOT match ``37-2024-00021082-CL-CL-CTL``."""
        html = self._build_html(self._make_row("37-2024-00021082-CL-CL-CTL"))
        section = extract_case_section(html, "24-00021082")
        assert section is None

    def test_returns_none_when_case_absent(self) -> None:
        html = self._build_html(self._make_row("37-2023-00099999-CL-CL-CTL"))
        assert extract_case_section(html, "2024-00021082") is None


# ---------------------------------------------------------------------------
# parse_document — metadata extraction (#2331)
# ---------------------------------------------------------------------------


class TestParseDocumentMetadata:
    """Tests for parse_document re-extracting metadata from calendar HTML (#2331)."""

    def test_extracts_department(self) -> None:
        """parse_document extracts department from HTML when not already set."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        html = _load_html("sd_calendar_central.html")
        doc = CapturedDocument(
            scraper_id="ca-sd-calendar",
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
        assert result.department == "C-60"

    def test_extracts_judge_name(self) -> None:
        """parse_document extracts judge name from HTML when not already set."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        html = _load_html("sd_calendar_central.html")
        doc = CapturedDocument(
            scraper_id="ca-sd-calendar",
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
        assert result.judge_name == "Matthew C. Braner"

    def test_extracts_hearing_date(self) -> None:
        """parse_document extracts hearing date from HTML when not already set."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        html = _load_html("sd_calendar_central.html")
        doc = CapturedDocument(
            scraper_id="ca-sd-calendar",
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
        assert result.hearing_date == datetime(2026, 3, 13)

    def test_extracts_case_title(self) -> None:
        """parse_document extracts case title from HTML when not already set."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        html = _load_html("sd_calendar_central.html")
        doc = CapturedDocument(
            scraper_id="ca-sd-calendar",
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
        assert result.case_title == "Aasi et al vs American Honda Motor Co Inc"

    def test_extracts_parties(self) -> None:
        """parse_document extracts parties from HTML when not already set."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        html = _load_html("sd_calendar_central.html")
        doc = CapturedDocument(
            scraper_id="ca-sd-calendar",
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
        assert len(result.parties) == 3
        names = {p["name"] for p in result.parties}
        assert "Sumayya Aasi" in names

    def test_extracts_motion_type(self) -> None:
        """parse_document extracts motion type (event_type) from HTML."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        html = _load_html("sd_calendar_central.html")
        doc = CapturedDocument(
            scraper_id="ca-sd-calendar",
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
        assert result.motion_type == "Motion Hearing"

    def test_preserves_existing_fields(self) -> None:
        """parse_document does not overwrite fields that are already populated."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        html = _load_html("sd_calendar_central.html")
        doc = CapturedDocument(
            scraper_id="ca-sd-calendar",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="http://example.com",
            capture_timestamp=datetime(2026, 3, 13),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="abc123",
            case_number="24CU016153C",
            department="PRESET-DEPT",
            judge_name="Preset Judge",
            ruling_text="preset ruling text",
        )
        result = scraper.parse_document(doc)
        # Existing fields should NOT be overwritten
        assert result.department == "PRESET-DEPT"
        assert result.judge_name == "Preset Judge"
        assert result.ruling_text == "preset ruling text"
        # But missing fields should be populated
        assert result.case_title == "Aasi et al vs American Honda Motor Co Inc"
        assert result.hearing_date == datetime(2026, 3, 13)

    def test_ruling_text_has_no_raw_html(self) -> None:
        """ruling_text should be plain text, not raw HTML (#2331)."""
        config = _make_config()
        scraper = SDCalendarScraper(config)

        from framework.models import CapturedDocument

        html = _load_html("sd_calendar_central.html")
        doc = CapturedDocument(
            scraper_id="ca-sd-calendar",
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
        # Must NOT contain raw HTML tags
        assert "<" not in result.ruling_text
        assert ">" not in result.ruling_text
        # Must contain structured case information
        assert "24CU016153C" in result.ruling_text
        assert "Department: C-60" in result.ruling_text

    def test_extraction_config_is_none_for_calendar(self) -> None:
        """SD calendar scraper uses ExtractionMethod.NONE (#2331)."""
        from framework.extraction_config import ExtractionMethod, get_county_extraction_config

        config = get_county_extraction_config("CA", "San Diego", scraper_id="ca-sd-calendar")
        assert config is not None
        assert config.method == ExtractionMethod.NONE


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


# ---------------------------------------------------------------------------
# is_sd_calendar_html — content detection (#2447)
# ---------------------------------------------------------------------------


class TestIsSDCalendarHtml:
    """Tests for the content-based SD calendar HTML detector."""

    def test_detects_central_fixture(self) -> None:
        html = _load_html("sd_calendar_central.html")
        assert is_sd_calendar_html(html) is True

    def test_detects_north_fixture(self) -> None:
        html = _load_html("sd_calendar_north.html")
        assert is_sd_calendar_html(html) is True

    def test_detects_empty_calendar_page(self) -> None:
        """Even an empty calendar page contains the CIVIL CALENDAR header."""
        html = _load_html("sd_calendar_empty.html")
        assert is_sd_calendar_html(html) is True

    def test_returns_false_for_non_calendar_html(self) -> None:
        assert is_sd_calendar_html("<html><body>ruling text</body></html>") is False

    def test_returns_false_for_plain_ruling_text(self) -> None:
        assert is_sd_calendar_html("TENTATIVE RULING: motion granted.") is False

    def test_returns_false_for_empty_string(self) -> None:
        assert is_sd_calendar_html("") is False

    def test_returns_false_for_none(self) -> None:
        assert is_sd_calendar_html(None) is False

    def test_matches_header_in_first_4kb(self) -> None:
        """The detector only scans the first 4 KB — padding past 4 KB fails."""
        padding = "x" * 5000
        html = padding + "<h1>CIVIL CALENDAR For Friday, 03/13/2026</h1>"
        assert is_sd_calendar_html(html) is False

    def test_case_sensitive_header(self) -> None:
        """The header marker is a literal substring (preserves case)."""
        # Real pages always use the exact capitalisation.
        html = "<h1>civil calendar for Friday</h1>"
        assert is_sd_calendar_html(html) is False


# ---------------------------------------------------------------------------
# _split_rulings — deterministic per-case splitter (#2447)
# ---------------------------------------------------------------------------


class TestSplitRulings:
    """Tests for the deterministic SD calendar splitter used by the worker
    and the reingest registry.

    The splitter MUST:
      * Return an empty list when the content is not a SD calendar page,
        so non-SD callers fall through to their default path.
      * Produce one ``SDSplitRuling`` per motion-type hearing.
      * Build a focused ``ruling_text`` per case (no raw HTML, no
        cross-contamination between cases, well below the 50000-char
        truncation cap).
      * Filter out non-motion events (CMCs, Ex Parte, Restraining Order,
        Name Change).
    """

    def test_returns_empty_for_non_calendar_html(self) -> None:
        """Non-calendar content returns an empty list (callers fall through)."""
        assert _split_rulings("<html><body>just some HTML</body></html>") == []

    def test_returns_empty_for_plain_ruling_text(self) -> None:
        assert _split_rulings("TENTATIVE RULING: motion granted.") == []

    def test_returns_empty_for_empty_string(self) -> None:
        assert _split_rulings("") == []

    def test_returns_empty_for_empty_calendar(self) -> None:
        """An SD calendar page with no hearings yields no rulings."""
        html = _load_html("sd_calendar_empty.html")
        assert _split_rulings(html) == []

    def test_splits_central_fixture_into_seven_rulings(self) -> None:
        """Central fixture has 7 motion-type hearings → 7 rulings.

        Non-motion events (CMC, Restraining Order, Ex Parte) are filtered
        out by the splitter even though they appear in the calendar HTML.
        """
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        assert len(rulings) == 7

    def test_all_results_are_sdsplitruling(self) -> None:
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        assert all(isinstance(r, SDSplitRuling) for r in rulings)

    def test_split_returns_all_motion_case_numbers(self) -> None:
        """Every motion-type case from the fixture is represented exactly once."""
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        case_numbers = [r.case_number for r in rulings]
        expected = {
            "24CU016153C",  # Motion Hearing (C-60)
            "25CU003887C",  # Demurrer (C-60)
            "23CU005421C",  # Summary Judgment (C-60)
            "25CU008912C",  # Discovery Hearing (C-65)
            "24CU019876C",  # Motion to Quash (C-65)
            "24CU012345C",  # Motion for Sanctions (C-67)
            "23CU007654C",  # Class Action (C-67)
        }
        assert set(case_numbers) == expected

    def test_split_filters_non_motion_events(self) -> None:
        """Non-motion events must NOT appear in the split output."""
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        case_numbers = {r.case_number for r in rulings}
        # These are the three non-motion events in the fixture:
        assert "26CL011234C" not in case_numbers  # Case Management Conference
        assert "25HR050123C" not in case_numbers  # Restraining Order
        assert "26CL015678C" not in case_numbers  # Ex Parte Hearing

    def test_ruling_text_is_focused_not_raw_html(self) -> None:
        """Each ruling's ruling_text is a plain-text excerpt (NO raw HTML).

        The critical bug in #2447 was that the LLM path substituted the
        entire 60-100KB HTML page as ruling_text.  The splitter MUST NOT
        emit HTML tags in ruling_text.
        """
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        for r in rulings:
            assert "<" not in r.ruling_text
            assert ">" not in r.ruling_text
            assert "<!doctype" not in r.ruling_text.lower()
            assert "<html" not in r.ruling_text.lower()
            assert "<body" not in r.ruling_text.lower()

    def test_ruling_text_lengths_are_well_below_50000(self) -> None:
        """Each focused excerpt is ~1KB — nowhere near the 50000-char cap.

        This is the direct regression test for Bug B in #2447 (stored
        ruling_text values of exactly 50000 chars).
        """
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        for r in rulings:
            assert len(r.ruling_text) < 50000, (
                f"ruling_text for {r.case_number} is {len(r.ruling_text)} chars "
                "— must be well under the 50000 truncation cap"
            )
            # Sanity: real focused excerpts are typically 200-2000 chars.
            assert len(r.ruling_text) < 10_000

    def test_ruling_text_contains_only_own_case_number(self) -> None:
        """No ruling's text may contain the case number of another ruling.

        This is the direct regression test for Bug A in #2447 (DB
        identifiers for case X while ruling_text body refers to case Y).
        """
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        all_case_numbers = {r.case_number for r in rulings if r.case_number}
        for r in rulings:
            own = r.case_number
            for other in all_case_numbers:
                if other == own:
                    continue
                assert other not in r.ruling_text, (
                    f"ruling_text for {own} leaked foreign case number {other}"
                )

    def test_split_preserves_case_title(self) -> None:
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        by_case = {r.case_number: r for r in rulings}
        assert by_case["24CU016153C"].case_title == ("Aasi et al vs American Honda Motor Co Inc")
        assert by_case["25CU003887C"].case_title == "Garcia vs City of San Diego"

    def test_split_preserves_department(self) -> None:
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        by_case = {r.case_number: r for r in rulings}
        assert by_case["24CU016153C"].department == "C-60"
        assert by_case["25CU008912C"].department == "C-65"
        assert by_case["24CU012345C"].department == "C-67"

    def test_split_preserves_motion_type(self) -> None:
        """motion_type carries the calendar's Event column verbatim."""
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        by_case = {r.case_number: r for r in rulings}
        assert by_case["24CU016153C"].motion_type == "Motion Hearing"
        assert by_case["25CU003887C"].motion_type == "Demurrer/Motion to Strike"
        assert by_case["23CU005421C"].motion_type == ("Summary Judgment/Summary Adjudication")
        assert by_case["25CU008912C"].motion_type == "Discovery Hearing"
        assert by_case["24CU019876C"].motion_type == "Motion to Quash"
        assert by_case["24CU012345C"].motion_type == "Motion for Sanctions"

    def test_split_preserves_hearing_date(self) -> None:
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        # Central fixture header is "Friday, 03/13/2026".
        assert all(r.hearing_date == datetime(2026, 3, 13) for r in rulings)

    def test_split_preserves_judge_name_when_real(self) -> None:
        """Real judge names propagate; generic 'Judge C-67 Central' becomes None."""
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        by_case = {r.case_number: r for r in rulings}
        # C-60 and C-65 have real judges.
        assert by_case["24CU016153C"].judge_name == "Matthew C. Braner"
        assert by_case["25CU008912C"].judge_name == "Karen S. Hewitt"
        # C-67 uses "Judge C-67 Central" — filtered to None.
        assert by_case["24CU012345C"].judge_name is None

    def test_split_preserves_parties(self) -> None:
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        by_case = {r.case_number: r for r in rulings}
        aasi = by_case["24CU016153C"]
        assert aasi.parties
        names = {p["name"] for p in aasi.parties}
        assert "Sumayya Aasi" in names
        assert "American Honda Motor Co Inc" in names

    def test_ruling_indices_are_1_based_and_sequential(self) -> None:
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        indices = [r.ruling_index for r in rulings]
        assert indices == [1, 2, 3, 4, 5, 6, 7]

    def test_outcome_is_none(self) -> None:
        """Calendar rows carry no ruling disposition yet."""
        html = _load_html("sd_calendar_central.html")
        rulings = _split_rulings(html)
        assert all(r.outcome is None for r in rulings)


# ---------------------------------------------------------------------------
# Extraction config — rebuild-ca-san_diego override (#2447)
# ---------------------------------------------------------------------------


class TestRebuildSanDiegoExtractionOverride:
    """Ensure the synthetic rebuild scraper_id gets ExtractionMethod.NONE.

    ``scripts/rebuild_db.py`` synthesises events with ``scraper_id`` set to
    ``rebuild-ca-san_diego``.  Without the override, the (CA, San Diego)
    county default (LLM) would fire on a 60-100KB calendar HTML page and
    produce the 50000-char raw-HTML dumps reported in #2447.
    """

    def test_rebuild_ca_san_diego_uses_none(self) -> None:
        from framework.extraction_config import ExtractionMethod, get_county_extraction_config

        config = get_county_extraction_config("CA", "San Diego", scraper_id="rebuild-ca-san_diego")
        assert config is not None
        assert config.method == ExtractionMethod.NONE

    def test_rebuild_override_registered_in_scraper_configs(self) -> None:
        from framework.extraction_config import _SCRAPER_CONFIGS, ExtractionMethod

        assert "rebuild-ca-san_diego" in _SCRAPER_CONFIGS
        assert _SCRAPER_CONFIGS["rebuild-ca-san_diego"].method == ExtractionMethod.NONE
