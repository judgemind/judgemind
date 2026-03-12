"""San Diego Superior Court — Civil Calendar Scraper (Phase 1).

Strategy: enumerate hearings from the court's public civil calendar system
using plain HTTP GET requests. Parse HTML tables to extract case metadata.
Filter for motion-type events likely to have tentative rulings.

This is Phase 1 of a two-phase scraper. Phase 1 enumerates cases from the
calendar; Phase 2 (future) retrieves tentative rulings from the Odyssey ROA
portal via Playwright.

Verified against live site 2026-03-11:
  URL: http://www.sandiego.courts.ca.gov/portal/online/calendar/
  4 divisions: Central, North County, East County, South County
  5-day rolling window per division (f_svcal{1-5}.html, etc.)
  No bot protection — plain HTTP GET returns full HTML
  No CAPTCHA, no cookies, no JavaScript required

Calendar HTML structure:
  <h1>CIVIL CALENDAR For Friday, 03/13/2026</h1>
  <h3>CENTRAL DIVISION, CENTRAL COURTHOUSE</h3>
  <div class="department">
    <h2><a name='C-60'></a>Department: C-60</h2>
    <table class="tables">
      <tr>
        <td>9:00 AM</td>       <!-- Time -->
        <td>24CU016153C</td>   <!-- Case# -->
        <td>Smith vs Jones</td> <!-- Entitlement -->
        <td>Motion Hearing</td> <!-- Event -->
        <td>Judge MATTHEW C. BRANER</td> <!-- Hearing Officer -->
        <td><p>(PL) John Smith</p><p>(DF) Robert Jones</p></td>  <!-- Party -->
        <td><p>Jane Doe</p><p>Pro Per</p></td>  <!-- Attorney -->
      </tr>
    </table>
  </div>

Party format: "(PL) Name" or "(PL) - Company Name" or "(DF) Name"
Judge format: "Judge FULL NAME" or generic "Judge C-60 Central"

Investigation: #154
Report: docs/investigations/san-diego-scraper-2026-03.md
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import structlog
from bs4 import BeautifulSoup, Tag

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig
from framework.events import EventBus
from framework.storage import S3Archiver

logger = structlog.get_logger(__name__)

CALENDAR_BASE_URL = "http://www.sandiego.courts.ca.gov/portal/online/calendar"

# Division URL patterns: (division_name, filename_prefix)
# Each division has pages numbered 1-5 for the rolling 5-business-day window.
DIVISIONS: list[tuple[str, str]] = [
    ("Central", "f_svcal"),
    ("North County", "F_VVCAL"),
    ("East County", "F_EVCAL"),
    ("South County", "F_BVCAL"),
]

# Motion-type events that typically have tentative rulings.
# Matched case-insensitively against the Event column.
MOTION_EVENT_TYPES: frozenset[str] = frozenset(
    s.lower()
    for s in (
        "Motion Hearing",
        "Demurrer/Motion to Strike",
        "Summary Judgment/Summary Adjudication",
        "Discovery Hearing",
        "Motion to Quash",
        "Motion for Sanctions",
        "Motion Hearing to Certify/Decertify Class Action",
    )
)

# Extract the date from the h1 header: "CIVIL CALENDAR For Friday, 03/13/2026"
_CALENDAR_DATE_RE = re.compile(
    r"CIVIL\s+CALENDAR\s+For\s+\w+,\s+(?P<date>\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)

# Extract department code from h2: "Department: C-60"
_DEPARTMENT_RE = re.compile(r"Department:\s*(?P<dept>\S+)")

# Extract division/courthouse from h3 text: "CENTRAL DIVISION, CENTRAL COURTHOUSE"
# or "NORTH COUNTY DIVISION" or "SOUTH COUNTY DIVISION"
_DIVISION_RE = re.compile(
    r"(?P<division>(?:CENTRAL|NORTH COUNTY|EAST COUNTY|SOUTH COUNTY)\s+DIVISION)"
    r"(?:,\s*(?P<courthouse>.+))?",
    re.IGNORECASE,
)

# Judge name: strip "Judge " prefix, e.g. "Judge MATTHEW C. BRANER" → "Matthew C. Braner"
# Generic names like "Judge 2102 Central" or "Judge C-60 Central" are filtered out.
_GENERIC_JUDGE_RE = re.compile(
    r"^Judge\s+(?:\w+-?\d+|\d+)\s+\w+$",
    re.IGNORECASE,
)

# Party role prefix: "(PL)" or "(DF)", optionally followed by " - "
_PARTY_RE = re.compile(
    r"^\((?P<role>PL|DF)\)\s*(?:-\s*)?(?P<name>.+)$",
    re.IGNORECASE,
)

# Role mapping from calendar abbreviations to normalized roles.
_ROLE_MAP: dict[str, str] = {
    "PL": "plaintiff",
    "DF": "defendant",
}


@dataclass
class CalendarHearing:
    """A single hearing entry parsed from the calendar HTML."""

    hearing_time: str
    case_number: str
    case_title: str
    event_type: str
    judge_name: str | None
    department: str
    division: str
    courthouse: str | None
    hearing_date: datetime | None
    parties: list[dict[str, str]] = field(default_factory=list)
    attorneys: list[str] = field(default_factory=list)


def _is_motion_event(event_type: str) -> bool:
    """Return True if the event type is a motion-type hearing."""
    return event_type.strip().lower() in MOTION_EVENT_TYPES


def _parse_judge_name(raw: str) -> str | None:
    """Parse judge name from the 'Hearing Officer' column.

    Returns None for generic department-based names like "Judge 2102 Central"
    or "Judge C-60 Central".  Strips "Judge " prefix and title-cases.
    """
    raw = raw.strip()
    if not raw:
        return None

    # Filter out generic names
    if _GENERIC_JUDGE_RE.match(raw):
        return None

    # Strip "Judge " prefix
    if raw.lower().startswith("judge "):
        raw = raw[6:]

    # Title-case if all uppercase
    name = raw.title() if raw.isupper() else raw
    return name.strip() or None


def _parse_parties(party_td: Tag) -> list[dict[str, str]]:
    """Parse party names and roles from the Party column.

    Each party is in a <p> tag like "(PL) John Smith" or "(DF) - Company Name".
    """
    parties: list[dict[str, str]] = []
    seen: set[str] = set()

    for p_tag in party_td.find_all("p"):
        text = p_tag.get_text(strip=True)
        m = _PARTY_RE.match(text)
        if not m:
            continue

        role_code = m.group("role").upper()
        name = m.group("name").strip()
        if not name:
            continue

        # Title-case if all uppercase
        if name.isupper():
            name = name.title()

        role = _ROLE_MAP.get(role_code, role_code.lower())
        key = name.lower()
        if key not in seen:
            seen.add(key)
            parties.append({"name": name, "role": role})

    return parties


def _parse_attorneys(attorney_td: Tag) -> list[str]:
    """Parse attorney names from the Attorney column.

    Each attorney is in a <p> tag.  "Pro Per" and "Unknown" are preserved
    as they indicate self-representation or unassigned counsel.
    """
    attorneys: list[str] = []
    for p_tag in attorney_td.find_all("p"):
        name = p_tag.get_text(strip=True)
        if name:
            attorneys.append(name)
    return attorneys


def _parse_calendar_date(html: str) -> datetime | None:
    """Extract the hearing date from the h1 header."""
    m = _CALENDAR_DATE_RE.search(html)
    if not m:
        return None
    try:
        return datetime.strptime(m.group("date"), "%m/%d/%Y")
    except ValueError:
        return None


def _parse_division_info(soup: BeautifulSoup) -> tuple[str, str | None]:
    """Extract division name and courthouse from the h3 header.

    Returns (division, courthouse) where courthouse may be None.
    """
    for h3 in soup.find_all("h3"):
        text = h3.get_text(strip=True)
        m = _DIVISION_RE.match(text)
        if m:
            division = m.group("division").strip().title()
            courthouse = m.group("courthouse")
            if courthouse:
                courthouse = courthouse.strip().title()
            return division, courthouse
    return "Unknown", None


def _clean_case_title(raw: str) -> str:
    """Clean up case title/entitlement text.

    Removes [IMAGED] suffix and normalizes whitespace.
    """
    title = re.sub(r"\s*\[IMAGED\]\s*$", "", raw, flags=re.IGNORECASE)
    return " ".join(title.split()).strip()


def parse_calendar_page(html: str) -> list[CalendarHearing]:
    """Parse a single calendar page HTML into a list of CalendarHearing objects.

    Extracts all hearings from all departments on the page, regardless of
    event type.  Filtering for motion-type events is done separately.
    """
    soup = BeautifulSoup(html, "lxml")
    hearing_date = _parse_calendar_date(html)
    division, courthouse = _parse_division_info(soup)
    hearings: list[CalendarHearing] = []

    for dept_div in soup.find_all("div", class_="department"):
        # Extract department code
        h2 = dept_div.find("h2")
        if not h2:
            continue
        dept_text = h2.get_text(strip=True)
        dept_match = _DEPARTMENT_RE.search(dept_text)
        department = dept_match.group("dept") if dept_match else "Unknown"

        # Parse all rows in the department table
        table = dept_div.find("table", class_="tables")
        if not table:
            continue

        tbody = table.find("tbody")
        if not tbody:
            continue

        for row in tbody.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 7:
                continue

            hearing_time = tds[0].get_text(strip=True)
            case_number = tds[1].get_text(strip=True)
            case_title = _clean_case_title(tds[2].get_text(strip=True))
            event_type = tds[3].get_text(strip=True)
            judge_raw = tds[4].get_text(strip=True)
            judge_name = _parse_judge_name(judge_raw)
            parties = _parse_parties(tds[5])
            attorneys = _parse_attorneys(tds[6])

            hearings.append(
                CalendarHearing(
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
                )
            )

    return hearings


class SDCalendarScraper(BaseScraper):
    """Enumerates San Diego civil calendar hearings and filters for motion events.

    Fetches the next business day's calendar pages for all 4 divisions,
    parses hearing metadata, and filters for motion-type events that
    typically have tentative rulings.
    """

    def __init__(
        self,
        config: ScraperConfig,
        archiver: S3Archiver | None = None,
        event_bus: EventBus | None = None,
        day_numbers: list[int] | None = None,
    ) -> None:
        super().__init__(config, archiver=archiver, event_bus=event_bus)
        # Which day numbers (1-5) to fetch.  Default: [2] (tomorrow).
        self._day_numbers = day_numbers or [2]

    def _build_urls(self) -> list[tuple[str, str]]:
        """Build (url, division_name) pairs for all divisions and day numbers."""
        urls: list[tuple[str, str]] = []
        for division_name, prefix in DIVISIONS:
            for day in self._day_numbers:
                url = f"{CALENDAR_BASE_URL}/{prefix}{day}.html"
                urls.append((url, division_name))
        return urls

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch calendar pages and return filtered motion-type hearings as documents."""
        docs: list[CapturedDocument] = []

        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            urls = self._build_urls()
            self._log.info("Fetching calendar pages", url_count=len(urls))

            for url, division_name in urls:
                time.sleep(self.config.request_delay_seconds)
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    html = response.text

                    hearings = parse_calendar_page(html)
                    motion_hearings = [h for h in hearings if _is_motion_event(h.event_type)]

                    self._log.info(
                        "Parsed calendar page",
                        division=division_name,
                        url=url,
                        total_hearings=len(hearings),
                        motion_hearings=len(motion_hearings),
                    )

                    for hearing in motion_hearings:
                        doc = self._make_base_doc(
                            source_url=url,
                            raw_content=html.encode("utf-8"),
                            content_format=ContentFormat.HTML,
                        )
                        doc.case_number = hearing.case_number
                        doc.case_title = hearing.case_title
                        doc.department = hearing.department
                        doc.courthouse = hearing.courthouse
                        doc.judge_name = hearing.judge_name
                        doc.hearing_date = hearing.hearing_date
                        doc.motion_type = hearing.event_type
                        doc.parties = hearing.parties
                        doc.extra["division"] = hearing.division
                        doc.extra["hearing_time"] = hearing.hearing_time
                        doc.extra["event_type"] = hearing.event_type
                        doc.extra["attorneys"] = hearing.attorneys
                        docs.append(doc)

                except Exception as exc:
                    self._log.error(
                        "Failed to fetch calendar page",
                        division=division_name,
                        url=url,
                        error=str(exc),
                    )

        return docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """No additional parsing needed — fields are populated in fetch_documents."""
        return doc


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default San Diego calendar scraper configuration."""
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
            ScheduleWindow(start=dtime(2, 0), end=dtime(3, 0)),  # 2 AM catch-up
        ],
        request_delay_seconds=1.0,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
