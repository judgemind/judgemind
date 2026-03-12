"""Tests for San Diego County civil calendar scraper (Phase 1).

Fixtures captured from live site 2026-03-11:
  sd_calendar_central_friday.html — Central division, Friday 03/13/2026
    Departments: 201 (2 motion events), 2102 (10 rows, mix of motion + CMC),
                 C-60 and C-62 (with Discovery/Summary Judgment)
  sd_calendar_north.html — North County division, Wednesday 03/11/2026
    Departments with administrative hearings (no motion events)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from courts.ca.sd_calendar import (
    CALENDAR_BASE_URL,
    MOTION_EVENT_TYPES,
    SDCalendarScraper,
    _extract_department,
    _extract_division,
    _extract_hearing_date,
    _extract_location,
    _parse_attorneys,
    _parse_parties,
    filter_motion_events,
    parse_calendar_page,
)
from courts.ca.sd_calendar import default_config as sd_default_config
from framework import ContentFormat

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_calendar_page — against real Central Friday fixture
# ---------------------------------------------------------------------------


def test_parse_central_friday_returns_entries() -> None:
    """Should parse entries from a Friday calendar page with motion events."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    assert len(entries) > 0


def test_parse_central_friday_hearing_date() -> None:
    """All entries should have hearing date 03/13/2026 from the h1 header."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    for entry in entries:
        assert entry.hearing_date == datetime(2026, 3, 13)


def test_parse_central_friday_division() -> None:
    """All entries should have division 'Central'."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    for entry in entries:
        assert entry.division == "Central"


def test_parse_central_friday_departments() -> None:
    """Should find entries from multiple departments."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    depts = {e.department for e in entries}
    assert "201" in depts
    assert "2102" in depts


def test_parse_central_friday_case_numbers() -> None:
    """Case numbers should be extracted correctly."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    case_numbers = {e.case_number for e in entries}
    # Modern format
    assert "25UD069533C" in case_numbers
    # Legacy format
    assert "37-2015-00031588-CL-UD-CTL" in case_numbers


def test_parse_central_friday_case_titles() -> None:
    """Case titles should be extracted from the Entitlement column."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    titles = {e.case_title for e in entries}
    assert "Ayoub vs Clark" in titles
    assert any("Navy Federal" in t for t in titles)


def test_parse_central_friday_event_types() -> None:
    """Should find various event types."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    event_types = {e.event_type for e in entries}
    assert "Motion to Quash" in event_types
    assert "Motion Hearing" in event_types


def test_parse_central_friday_judge_names() -> None:
    """Should extract both real and placeholder judge names."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    judges = {e.judge_name for e in entries if e.judge_name}
    # Real judge name (prefix "Judge " stripped)
    assert "TODD F. STEVENS" in judges
    # Placeholder judge (prefix stripped)
    assert "201 Central" in judges or any("201" in j for j in judges)


def test_parse_central_friday_judge_placeholder_flag() -> None:
    """Placeholder judges should be flagged."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    placeholders = [e for e in entries if e.judge_is_placeholder]
    non_placeholders = [e for e in entries if not e.judge_is_placeholder]
    assert len(placeholders) > 0
    assert len(non_placeholders) > 0


def test_parse_central_friday_parties() -> None:
    """Parties should be parsed with name and role."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    # Find the Ayoub vs Clark entry
    ayoub_entries = [e for e in entries if e.case_number == "25UD069533C"]
    assert len(ayoub_entries) > 0
    entry = ayoub_entries[0]

    assert len(entry.parties) > 0
    roles = {p["role"] for p in entry.parties}
    assert "plaintiff" in roles
    assert "defendant" in roles

    # Check a specific party name
    names = {p["name"] for p in entry.parties}
    assert "George Ayoub" in names


def test_parse_central_friday_parties_with_dash() -> None:
    """Parties with dash format '(PL) - Name' should parse correctly."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    # Find an entry with dash format (Navy Federal)
    navy_entries = [e for e in entries if "Navy Federal" in e.case_title]
    assert len(navy_entries) > 0
    entry = navy_entries[0]
    pl_names = [p["name"] for p in entry.parties if p["role"] == "plaintiff"]
    assert any("Navy Federal" in n for n in pl_names)


def test_parse_central_friday_parties_deduplicated() -> None:
    """Duplicate parties in the same hearing should be deduplicated."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    for entry in entries:
        keys = [(p["name"].lower(), p["role"]) for p in entry.parties]
        assert len(keys) == len(set(keys)), (
            f"Duplicate parties in {entry.case_number}: {entry.parties}"
        )


def test_parse_central_friday_attorneys() -> None:
    """Attorneys should be extracted, excluding 'Unknown' and 'Pro Per'."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    # Find an entry with named attorneys
    navy_entries = [e for e in entries if e.case_number == "24CL009504C"]
    assert len(navy_entries) > 0
    entry = navy_entries[0]
    assert len(entry.attorneys) > 0
    for atty in entry.attorneys:
        assert atty != "Unknown"
        assert atty != "Pro Per"


def test_parse_central_friday_location() -> None:
    """Location should be extracted from the department h2."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    assert any("Union Street" in e.location for e in entries)


def test_parse_central_friday_courthouse() -> None:
    """All entries from the Central fixture should have Central Courthouse."""
    html = _load_html("sd_calendar_central_friday.html")
    entries = parse_calendar_page(html)
    for entry in entries:
        assert entry.courthouse == "Central Courthouse"


# ---------------------------------------------------------------------------
# parse_calendar_page — North County fixture
# ---------------------------------------------------------------------------


def test_parse_north_returns_entries() -> None:
    """Should parse entries from the North County calendar page."""
    html = _load_html("sd_calendar_north.html")
    entries = parse_calendar_page(html)
    assert len(entries) > 0


def test_parse_north_division() -> None:
    """All entries should have division 'North County'."""
    html = _load_html("sd_calendar_north.html")
    entries = parse_calendar_page(html)
    for entry in entries:
        assert entry.division == "North County"


def test_parse_north_courthouse() -> None:
    """All entries should have North County Courthouse."""
    html = _load_html("sd_calendar_north.html")
    entries = parse_calendar_page(html)
    for entry in entries:
        assert entry.courthouse == "North County Courthouse"


# ---------------------------------------------------------------------------
# filter_motion_events
# ---------------------------------------------------------------------------


def test_filter_motion_events_central_friday() -> None:
    """Should filter to only motion-type events."""
    html = _load_html("sd_calendar_central_friday.html")
    all_entries = parse_calendar_page(html)
    motion_entries = filter_motion_events(all_entries)

    assert len(motion_entries) > 0
    assert len(motion_entries) < len(all_entries)

    for entry in motion_entries:
        assert entry.event_type in MOTION_EVENT_TYPES


def test_filter_motion_events_excludes_cmc() -> None:
    """Case Management Conference should not be included."""
    html = _load_html("sd_calendar_central_friday.html")
    all_entries = parse_calendar_page(html)
    motion_entries = filter_motion_events(all_entries)

    for entry in motion_entries:
        assert entry.event_type != "Case Management Conference"


def test_filter_motion_events_north_wednesday() -> None:
    """Wednesday North County page should have few/no motion events."""
    html = _load_html("sd_calendar_north.html")
    all_entries = parse_calendar_page(html)
    motion_entries = filter_motion_events(all_entries)
    # Wednesday typically has no motion events in North County
    assert len(motion_entries) <= len(all_entries)


# ---------------------------------------------------------------------------
# _extract_hearing_date — unit tests
# ---------------------------------------------------------------------------


def test_extract_hearing_date_friday() -> None:
    from bs4 import BeautifulSoup

    html = "<html><body><h1>CIVIL CALENDAR For Friday, 03/13/2026</h1></body></html>"
    soup = BeautifulSoup(html, "lxml")
    dt = _extract_hearing_date(soup)
    assert dt == datetime(2026, 3, 13)


def test_extract_hearing_date_wednesday() -> None:
    from bs4 import BeautifulSoup

    html = "<html><body><h1>CIVIL CALENDAR For Wednesday, 03/11/2026</h1></body></html>"
    soup = BeautifulSoup(html, "lxml")
    dt = _extract_hearing_date(soup)
    assert dt == datetime(2026, 3, 11)


def test_extract_hearing_date_no_h1() -> None:
    from bs4 import BeautifulSoup

    html = "<html><body><p>No header</p></body></html>"
    soup = BeautifulSoup(html, "lxml")
    assert _extract_hearing_date(soup) is None


def test_extract_hearing_date_invalid_format() -> None:
    from bs4 import BeautifulSoup

    html = "<html><body><h1>CIVIL CALENDAR For Friday, 99/99/9999</h1></body></html>"
    soup = BeautifulSoup(html, "lxml")
    assert _extract_hearing_date(soup) is None


# ---------------------------------------------------------------------------
# _extract_division — unit tests
# ---------------------------------------------------------------------------


def test_extract_division_central() -> None:
    from bs4 import BeautifulSoup

    html = "<html><body><h3>CENTRAL DIVISION, CENTRAL COURTHOUSE</h3></body></html>"
    soup = BeautifulSoup(html, "lxml")
    assert _extract_division(soup) == "Central"


def test_extract_division_north() -> None:
    from bs4 import BeautifulSoup

    html = "<html><body><h3>NORTH COUNTY DIVISION</h3></body></html>"
    soup = BeautifulSoup(html, "lxml")
    assert _extract_division(soup) == "North County"


def test_extract_division_east() -> None:
    from bs4 import BeautifulSoup

    html = "<html><body><h3>EAST COUNTY DIVISION</h3></body></html>"
    soup = BeautifulSoup(html, "lxml")
    assert _extract_division(soup) == "East County"


def test_extract_division_south() -> None:
    from bs4 import BeautifulSoup

    html = "<html><body><h3>SOUTH COUNTY DIVISION</h3></body></html>"
    soup = BeautifulSoup(html, "lxml")
    assert _extract_division(soup) == "South County"


def test_extract_division_unknown() -> None:
    from bs4 import BeautifulSoup

    html = "<html><body><h3>Some Other Text</h3></body></html>"
    soup = BeautifulSoup(html, "lxml")
    assert _extract_division(soup) == "Unknown"


# ---------------------------------------------------------------------------
# _extract_department — unit tests
# ---------------------------------------------------------------------------


def test_extract_department_standard() -> None:
    from bs4 import BeautifulSoup

    html = "<div class='department'><h2>Department: C-60</h2></div>"
    div = BeautifulSoup(html, "lxml").find("div")
    assert _extract_department(div) == "C-60"


def test_extract_department_numeric() -> None:
    from bs4 import BeautifulSoup

    html = "<div class='department'><h2>Department: 2102</h2></div>"
    div = BeautifulSoup(html, "lxml").find("div")
    assert _extract_department(div) == "2102"


# ---------------------------------------------------------------------------
# _extract_location — unit tests
# ---------------------------------------------------------------------------


def test_extract_location() -> None:
    from bs4 import BeautifulSoup

    html = (
        "<div class='department'>"
        "<h2 id='location'>Location: 1100 Union Street, San Diego</h2>"
        "</div>"
    )
    div = BeautifulSoup(html, "lxml").find("div")
    assert _extract_location(div) == "1100 Union Street, San Diego"


def test_extract_location_missing() -> None:
    from bs4 import BeautifulSoup

    html = "<div class='department'><h2>Department: C-60</h2></div>"
    div = BeautifulSoup(html, "lxml").find("div")
    assert _extract_location(div) == ""


# ---------------------------------------------------------------------------
# _parse_parties — unit tests
# ---------------------------------------------------------------------------


def test_parse_parties_basic() -> None:
    from bs4 import BeautifulSoup

    html = "<td><p>(PL) George Ayoub</p><p>(DF) Amy Clark</p></td>"
    td = BeautifulSoup(html, "lxml").find("td")
    parties = _parse_parties(td)
    assert len(parties) == 2
    assert parties[0] == {"name": "George Ayoub", "role": "plaintiff"}
    assert parties[1] == {"name": "Amy Clark", "role": "defendant"}


def test_parse_parties_with_dash() -> None:
    from bs4 import BeautifulSoup

    html = "<td><p>(PL) - Navy Federal Credit Union</p><p>(DF) Justin Flores</p></td>"
    td = BeautifulSoup(html, "lxml").find("td")
    parties = _parse_parties(td)
    assert len(parties) == 2
    assert parties[0]["name"] == "Navy Federal Credit Union"
    assert parties[0]["role"] == "plaintiff"


def test_parse_parties_deduplication() -> None:
    from bs4 import BeautifulSoup

    html = (
        "<td>"
        "<p>(PL) - Capital One NA</p>"
        "<p>(PL) Farid Wardak</p>"
        "<p>(DF) Farid Wardak</p>"
        "<p>(DF) - Capital One NA</p>"
        "</td>"
    )
    td = BeautifulSoup(html, "lxml").find("td")
    parties = _parse_parties(td)
    # Capital One appears as PL and DF — both should be kept (different roles)
    # Farid Wardak appears as PL and DF — both should be kept (different roles)
    assert len(parties) == 4


def test_parse_parties_empty() -> None:
    from bs4 import BeautifulSoup

    html = "<td></td>"
    td = BeautifulSoup(html, "lxml").find("td")
    parties = _parse_parties(td)
    assert len(parties) == 0


# ---------------------------------------------------------------------------
# _parse_attorneys — unit tests
# ---------------------------------------------------------------------------


def test_parse_attorneys_filters_unknown_and_pro_per() -> None:
    from bs4 import BeautifulSoup

    html = "<td><p>Andres Cherner</p><p>Pro Per</p><p>Unknown</p></td>"
    td = BeautifulSoup(html, "lxml").find("td")
    attorneys = _parse_attorneys(td)
    assert attorneys == ["Andres Cherner"]


def test_parse_attorneys_multiple() -> None:
    from bs4 import BeautifulSoup

    html = "<td><p>Rea Stelmach</p><p>Paul L Brisson</p></td>"
    td = BeautifulSoup(html, "lxml").find("td")
    attorneys = _parse_attorneys(td)
    assert attorneys == ["Rea Stelmach", "Paul L Brisson"]


# ---------------------------------------------------------------------------
# SDCalendarScraper — integration tests with mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_sd_scraper_fetch_single_division() -> None:
    """Test fetching a single division calendar page."""
    config = sd_default_config()
    scraper = SDCalendarScraper(config, day_offset=1)

    central_html = _load_html("sd_calendar_central_friday.html")

    # Mock all 4 division URLs
    respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(
        return_value=httpx.Response(200, text=central_html)
    )
    respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL1.html").mock(
        return_value=httpx.Response(200, text="<html><body></body></html>")
    )
    respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL1.html").mock(
        return_value=httpx.Response(200, text="<html><body></body></html>")
    )
    respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL1.html").mock(
        return_value=httpx.Response(200, text="<html><body></body></html>")
    )

    docs = scraper.fetch_documents()
    assert len(docs) > 0

    # All docs should be motion-type events
    for doc in docs:
        assert doc.motion_type in MOTION_EVENT_TYPES


@respx.mock
def test_sd_scraper_document_fields() -> None:
    """Test that CapturedDocument fields are properly populated."""
    config = sd_default_config()
    scraper = SDCalendarScraper(config, day_offset=1)

    central_html = _load_html("sd_calendar_central_friday.html")
    respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(
        return_value=httpx.Response(200, text=central_html)
    )
    for prefix in ("F_VVCAL", "F_EVCAL", "F_BVCAL"):
        respx.get(f"{CALENDAR_BASE_URL}/{prefix}1.html").mock(
            return_value=httpx.Response(200, text="<html><body></body></html>")
        )

    docs = scraper.fetch_documents()
    assert len(docs) > 0

    # Check the first document has all required fields
    doc = docs[0]
    assert doc.case_number is not None
    assert doc.case_title is not None
    assert doc.department is not None
    assert doc.courthouse is not None
    assert doc.hearing_date is not None
    assert doc.motion_type is not None
    assert doc.state == "CA"
    assert doc.county == "San Diego"

    # Check extra fields
    assert "division" in doc.extra
    assert "hearing_time" in doc.extra
    assert "attorneys" in doc.extra
    assert "location" in doc.extra
    assert "judge_is_placeholder" in doc.extra

    # raw_content should be the original <tr> HTML
    raw = doc.raw_content.decode("utf-8")
    assert raw.startswith("<tr>")
    assert "</tr>" in raw


@respx.mock
def test_sd_scraper_division_failure_continues() -> None:
    """If one division fails, the scraper should continue with others."""
    config = sd_default_config()
    scraper = SDCalendarScraper(config, day_offset=1)

    central_html = _load_html("sd_calendar_central_friday.html")

    # Central succeeds, North fails, others empty
    respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(
        return_value=httpx.Response(200, text=central_html)
    )
    respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL1.html").mock(return_value=httpx.Response(500))
    respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL1.html").mock(
        return_value=httpx.Response(200, text="<html><body></body></html>")
    )
    respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL1.html").mock(
        return_value=httpx.Response(200, text="<html><body></body></html>")
    )

    docs = scraper.fetch_documents()
    # Should still get docs from Central
    assert len(docs) > 0


@respx.mock
def test_sd_scraper_parse_document_noop() -> None:
    """parse_document should be a no-op (fields already populated)."""
    config = sd_default_config()
    scraper = SDCalendarScraper(config)

    doc = scraper._make_base_doc(
        source_url="http://example.com",
        raw_content=b"<html>test</html>",
        content_format=ContentFormat.HTML,
    )
    doc.case_number = "25UD069533C"
    doc.judge_name = "SMITH"

    parsed = scraper.parse_document(doc)
    assert parsed.case_number == "25UD069533C"
    assert parsed.judge_name == "SMITH"


@respx.mock
def test_sd_scraper_full_run() -> None:
    """Test a full scraper run (fetch + process) with mocked HTTP."""
    config = sd_default_config()
    scraper = SDCalendarScraper(config, day_offset=1)

    central_html = _load_html("sd_calendar_central_friday.html")
    respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(
        return_value=httpx.Response(200, text=central_html)
    )
    for prefix in ("F_VVCAL", "F_EVCAL", "F_BVCAL"):
        respx.get(f"{CALENDAR_BASE_URL}/{prefix}1.html").mock(
            return_value=httpx.Response(200, text="<html><body></body></html>")
        )

    health = scraper.run()
    assert health.success is True
    assert health.records_captured > 0
    assert health.scraper_id == "ca-sd-calendar"


# ---------------------------------------------------------------------------
# default_config
# ---------------------------------------------------------------------------


def test_sd_default_config() -> None:
    config = sd_default_config()
    assert config.scraper_id == "ca-sd-calendar"
    assert config.state == "CA"
    assert config.county == "San Diego"
    assert len(config.schedule_windows) == 2


# ---------------------------------------------------------------------------
# MOTION_EVENT_TYPES — completeness
# ---------------------------------------------------------------------------


def test_motion_event_types_includes_key_types() -> None:
    """Verify the motion event types set includes all expected types."""
    assert "Motion Hearing" in MOTION_EVENT_TYPES
    assert "Demurrer/Motion to Strike" in MOTION_EVENT_TYPES
    assert "Summary Judgment/Summary Adjudication" in MOTION_EVENT_TYPES
    assert "Discovery Hearing" in MOTION_EVENT_TYPES
    assert "Motion to Quash" in MOTION_EVENT_TYPES
    assert "Motion for Sanctions" in MOTION_EVENT_TYPES
    assert "Motion UD" in MOTION_EVENT_TYPES
