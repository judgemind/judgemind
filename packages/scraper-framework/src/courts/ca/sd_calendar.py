"""San Diego Superior Court — Civil Calendar Scraper (Phase 1).

Enumerates civil hearings from the court's public calendar system and filters
for motion-type events that are likely to have tentative rulings.

Calendar system: http://www.sandiego.courts.ca.gov/portal/online/calendar/
No bot protection — plain HTTP GET requests return full HTML.

Four divisions, each with 5 rolling business-day pages:
  Central: f_svcal{1-5}.html
  North:   F_VVCAL{1-5}.html
  East:    F_EVCAL{1-5}.html
  South:   F_BVCAL{1-5}.html

HTML structure per page:
  <h1>CIVIL CALENDAR For Friday, 03/13/2026</h1>
  <h3>CENTRAL DIVISION, CENTRAL COURTHOUSE</h3>
  <div class="department">
    <h2>Department: C-60</h2>
    <table class="tables">  (columns: Time, Case#, Entitlement, Event,
                              Hearing Officer, Party, Attorney)

Party format: <p>(PL) - Capital One NA</p> or <p>(DF) Farid Wardak</p>
Judge format: "Judge TODD F. STEVENS" or "Judge 2102 Central" (placeholder)

Investigation: #154
Report: docs/investigations/san-diego-scraper-2026-03.md
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
import structlog
from bs4 import BeautifulSoup, Tag

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig
from framework.events import EventBus
from framework.storage import S3Archiver

logger = structlog.get_logger(__name__)

CALENDAR_BASE_URL = "http://www.sandiego.courts.ca.gov/portal/online/calendar"

# Division configurations: (division_name, url_prefix, courthouse_name)
DIVISIONS: list[tuple[str, str, str]] = [
    ("Central", "f_svcal", "Central Courthouse"),
    ("North County", "F_VVCAL", "North County Courthouse"),
    ("East County", "F_EVCAL", "East County Courthouse"),
    ("South County", "F_BVCAL", "South County Courthouse"),
]

# Event types that typically have tentative rulings.
MOTION_EVENT_TYPES: set[str] = {
    "Motion Hearing",
    "Demurrer/Motion to Strike",
    "Summary Judgment/Summary Adjudication",
    "Discovery Hearing",
    "Motion to Quash",
    "Motion for Sanctions",
    "Motion UD",
}

# Date from the h1 header: "CIVIL CALENDAR For Friday, 03/13/2026"
_HEADER_DATE_RE = re.compile(
    r"CIVIL CALENDAR For \w+,\s*(\d{2}/\d{2}/\d{4})",
)

# Party line: "(PL) - Capital One NA" or "(DF) Farid Wardak"
_PARTY_RE = re.compile(
    r"\((?P<role>PL|DF)\)\s*-?\s*(?P<name>.+)",
)

# Judge placeholder pattern: "Judge 2102 Central", "Judge 201 Central"
_JUDGE_PLACEHOLDER_RE = re.compile(
    r"^Judge\s+\d+\s+\w+$",
)

# Division from h3: "CENTRAL DIVISION, CENTRAL COURTHOUSE" or "NORTH COUNTY DIVISION"
_DIVISION_RE = re.compile(
    r"(?P<division>CENTRAL|NORTH COUNTY|EAST COUNTY|SOUTH COUNTY)\s+DIVISION",
    re.IGNORECASE,
)


@dataclass
class CalendarEntry:
    """A single hearing entry parsed from the calendar HTML."""

    hearing_time: str
    case_number: str
    case_title: str
    event_type: str
    judge_name: str | None
    department: str
    division: str
    courthouse: str
    hearing_date: datetime | None
    parties: list[dict[str, str]]
    attorneys: list[str]
    location: str
    judge_is_placeholder: bool
    row_html: str = ""  # original <tr> HTML for raw_content archival


def parse_calendar_page(html: str) -> list[CalendarEntry]:
    """Parse a single calendar HTML page into a list of CalendarEntry objects.

    Extracts the hearing date from the page header, then iterates over all
    department sections and their table rows.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[CalendarEntry] = []

    # Extract hearing date from h1 header
    hearing_date = _extract_hearing_date(soup)

    # Extract division from h3
    division = _extract_division(soup)

    # Map division to courthouse name
    courthouse = _division_to_courthouse(division)

    # Process each department section
    for dept_div in soup.find_all("div", class_="department"):
        dept_name = _extract_department(dept_div)
        location = _extract_location(dept_div)

        table = dept_div.find("table", class_="tables")
        if not table:
            continue

        tbody = table.find("tbody")
        if not tbody:
            continue

        for row in tbody.find_all("tr"):
            entry = _parse_row(row, dept_name, division, courthouse, hearing_date, location)
            if entry is not None:
                entries.append(entry)

    return entries


def filter_motion_events(entries: list[CalendarEntry]) -> list[CalendarEntry]:
    """Filter calendar entries to only include motion-type events."""
    return [e for e in entries if e.event_type in MOTION_EVENT_TYPES]


def _extract_hearing_date(soup: BeautifulSoup) -> datetime | None:
    """Extract the hearing date from the page h1 header."""
    h1 = soup.find("h1")
    if h1 is None:
        return None
    m = _HEADER_DATE_RE.search(h1.get_text())
    if m is None:
        return None
    try:
        return datetime.strptime(m.group(1), "%m/%d/%Y")
    except ValueError:
        return None


def _extract_division(soup: BeautifulSoup) -> str:
    """Extract the division name from the h3 header."""
    for h3 in soup.find_all("h3"):
        text = h3.get_text(strip=True)
        m = _DIVISION_RE.search(text)
        if m:
            # Normalize: "CENTRAL" -> "Central", "NORTH COUNTY" -> "North County"
            return m.group("division").title()
    return "Unknown"


def _division_to_courthouse(division: str) -> str:
    """Map division name to courthouse name."""
    mapping = {
        "Central": "Central Courthouse",
        "North County": "North County Courthouse",
        "East County": "East County Courthouse",
        "South County": "South County Courthouse",
    }
    return mapping.get(division, "Unknown Courthouse")


def _extract_department(dept_div: Tag) -> str:
    """Extract department name from the department div's h2 tag."""
    h2 = dept_div.find("h2")
    if h2 is None:
        return "Unknown"
    text = h2.get_text(strip=True)
    # Format: "Department: C-60" or "Department: 2102"
    m = re.search(r"Department:\s*(\S+)", text)
    return m.group(1) if m else "Unknown"


def _extract_location(dept_div: Tag) -> str:
    """Extract location from the department div's location h2 tag."""
    loc_h2 = dept_div.find("h2", id="location")
    if loc_h2 is None:
        return ""
    text = loc_h2.get_text(strip=True)
    # Format: "Location: 1100 Union Street, San Diego"
    m = re.search(r"Location:\s*(.+)", text)
    return m.group(1).strip() if m else ""


def _parse_row(
    row: Tag,
    department: str,
    division: str,
    courthouse: str,
    hearing_date: datetime | None,
    location: str,
) -> CalendarEntry | None:
    """Parse a single table row into a CalendarEntry."""
    tds = row.find_all("td")
    if len(tds) < 7:
        return None

    hearing_time = tds[0].get_text(strip=True)
    case_number = tds[1].get_text(strip=True)
    case_title = tds[2].get_text(strip=True)
    event_type = tds[3].get_text(strip=True)

    # Judge name
    raw_judge = tds[4].get_text(strip=True)
    judge_name: str | None = None
    judge_is_placeholder = False
    if raw_judge:
        # Strip "Judge " prefix
        name = re.sub(r"^Judge\s+", "", raw_judge).strip()
        if _JUDGE_PLACEHOLDER_RE.match(raw_judge):
            judge_is_placeholder = True
            judge_name = name
        else:
            judge_name = name

    # Parties
    parties = _parse_parties(tds[5])

    # Attorneys
    attorneys = _parse_attorneys(tds[6])

    # Preserve original <tr> HTML for raw_content archival
    row_html = str(row)

    return CalendarEntry(
        hearing_time=hearing_time,
        case_number=case_number,
        case_title=case_title,
        event_type=event_type,
        judge_name=judge_name,
        department=department,
        division=division,
        courthouse=courthouse,
        hearing_date=hearing_date,
        parties=parties,
        attorneys=attorneys,
        location=location,
        judge_is_placeholder=judge_is_placeholder,
        row_html=row_html,
    )


def _parse_parties(td: Tag) -> list[dict[str, str]]:
    """Parse party information from the Party column td.

    Format: <p>(PL) - Capital One NA</p> or <p>(DF) Farid Wardak</p>
    Role mapping: PL -> plaintiff, DF -> defendant
    """
    parties: list[dict[str, str]] = []
    seen: set[str] = set()
    role_map = {"PL": "plaintiff", "DF": "defendant"}

    for p_tag in td.find_all("p"):
        text = p_tag.get_text(strip=True)
        m = _PARTY_RE.match(text)
        if m:
            role_code = m.group("role")
            name = m.group("name").strip()
            role = role_map.get(role_code, role_code.lower())

            # Deduplicate by (name_lower, role)
            key = (name.lower(), role)
            if key not in seen:
                seen.add(key)
                parties.append({"name": name, "role": role})

    return parties


def _parse_attorneys(td: Tag) -> list[str]:
    """Parse attorney names from the Attorney column td."""
    attorneys: list[str] = []
    for p_tag in td.find_all("p"):
        text = p_tag.get_text(strip=True)
        if text and text not in ("Unknown", "Pro Per"):
            attorneys.append(text)
    return attorneys


class SDCalendarScraper(BaseScraper):
    """San Diego County civil calendar scraper — Phase 1.

    Fetches civil calendar pages for all 4 divisions and extracts hearings
    that match motion-type event types (likely to have tentative rulings).
    """

    def __init__(
        self,
        config: ScraperConfig,
        archiver: S3Archiver | None = None,
        event_bus: EventBus | None = None,
        day_offset: int = 2,
    ) -> None:
        super().__init__(config, archiver=archiver, event_bus=event_bus)
        # day_offset: which day page to fetch (1 = today, 2 = tomorrow, etc.)
        self._day_offset = day_offset

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch calendar pages for all divisions and return motion-type entries."""
        docs: list[CapturedDocument] = []

        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            for div_name, url_prefix, _courthouse in DIVISIONS:
                url = f"{CALENDAR_BASE_URL}/{url_prefix}{self._day_offset}.html"
                self._log.info("Fetching calendar", division=div_name, url=url)

                try:
                    response = client.get(url)
                    response.raise_for_status()
                except Exception as exc:
                    self._log.error(
                        "Failed to fetch calendar",
                        division=div_name,
                        url=url,
                        error=str(exc),
                    )
                    continue

                raw_html = response.text
                entries = parse_calendar_page(raw_html)
                motion_entries = filter_motion_events(entries)

                self._log.info(
                    "Parsed calendar",
                    division=div_name,
                    total_entries=len(entries),
                    motion_entries=len(motion_entries),
                )

                for entry in motion_entries:
                    doc = self._entry_to_document(entry, url)
                    docs.append(doc)

                time.sleep(self.config.request_delay_seconds)

        return docs

    def _entry_to_document(
        self,
        entry: CalendarEntry,
        source_url: str,
    ) -> CapturedDocument:
        """Convert a CalendarEntry into a CapturedDocument."""
        doc = self._make_base_doc(
            source_url=source_url,
            raw_content=entry.row_html.encode("utf-8"),
            content_format=ContentFormat.HTML,
        )

        doc.case_number = entry.case_number
        doc.case_title = entry.case_title
        doc.department = entry.department
        doc.courthouse = entry.courthouse
        doc.hearing_date = entry.hearing_date
        doc.judge_name = entry.judge_name
        doc.motion_type = entry.event_type
        doc.parties = entry.parties

        doc.extra["division"] = entry.division
        doc.extra["hearing_time"] = entry.hearing_time
        doc.extra["attorneys"] = entry.attorneys
        doc.extra["location"] = entry.location
        doc.extra["judge_is_placeholder"] = entry.judge_is_placeholder

        return doc

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Parse is a no-op for calendar entries — fields are already extracted."""
        # All structured fields are populated during fetch_documents.
        # The ruling text is not available from the calendar (that's Phase 2).
        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Create default ScraperConfig for the San Diego calendar scraper."""
    from datetime import time as dtime

    from framework import ScheduleWindow

    return ScraperConfig(
        scraper_id="ca-sd-calendar",
        state="CA",
        county="San Diego",
        court="Superior Court",
        target_urls=[CALENDAR_BASE_URL],
        poll_interval_seconds=86400,  # daily
        schedule_windows=[
            ScheduleWindow(start=dtime(16, 30), end=dtime(17, 30)),  # 4:30 PM primary
            ScheduleWindow(start=dtime(21, 0), end=dtime(22, 0)),  # 9 PM catch-up
        ],
        request_delay_seconds=1.0,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
