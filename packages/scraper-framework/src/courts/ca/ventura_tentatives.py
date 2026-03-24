"""Ventura County Superior Court — Civil Tentative Rulings Scraper.

Pattern: ASP.NET MVC search application with anti-forgery token and date-based
POST search. Returns an HTML table with pre-parsed structured fields.

Search URL: https://www2.ventura.courts.ca.gov/CaseInquiry/TentativeRulings
Method:
  1. GET the search page to establish a session and extract __RequestVerificationToken
  2. POST with SearchFromDate=M/D/YYYY to get results table
  3. Parse table rows: case number, event type (motion type), event datetime, department
  4. For each row with a ViewFile link: GET /CaseInquiry/ViewFile/{id} to download document

Results table columns:
  Case Number | Event Type | Event Date/Time | Department | Document

Case number format: 4-digit year + case type code + 6 digits
  e.g. 2025CUBC040123, 2024CUBC038456, 2025CUPR042345

Event types (pre-parsed motion types):
  Demurrer to Complaint, Motion to Compel Further Responses,
  Motion for Summary Judgment, Motion to Strike, Petition for Probate, etc.

Departments observed: 20, 42, 43 (civil), plus probate departments

Judge names: Not available in the results table. The scraper does not attempt
to extract judge names from downloaded documents since the document format is
inconsistent (some PDF, some HTML). The judge_name field will be left as None
unless a CourtDirectory snapshot provides the mapping.

Investigation: #155
Parent: #155
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from bs4 import BeautifulSoup

from framework import BaseScraper, CapturedDocument, ContentFormat, ScheduleWindow, ScraperConfig

logger = structlog.get_logger(__name__)

SEARCH_URL = "https://www2.ventura.courts.ca.gov/CaseInquiry/TentativeRulings"
BASE_URL = "https://www2.ventura.courts.ca.gov"

# Anti-forgery token hidden input
_TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"\s+type="hidden"\s+value="([^"]+)"'
    r'|type="hidden"\s+name="__RequestVerificationToken"\s+value="([^"]+)"',
)

# Case number: 4-digit year + type code + 6 digits
_CASE_NUMBER_RE = re.compile(r"\b\d{4}[A-Z]{4}\d{6}\b")

# Event datetime: MM/DD/YYYY H:MM AM/PM
_EVENT_DATETIME_RE = re.compile(
    r"(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)",
    re.IGNORECASE,
)

# ViewFile link: /CaseInquiry/ViewFile/{numeric_id}
_VIEWFILE_RE = re.compile(r"/CaseInquiry/ViewFile/(\d+)")


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------


def extract_verification_token(html: str) -> str | None:
    """Extract the __RequestVerificationToken from the search page HTML."""
    m = _TOKEN_RE.search(html)
    if m:
        return m.group(1) or m.group(2)
    # Fallback: parse with BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    token_input = soup.find("input", {"name": "__RequestVerificationToken"})
    if token_input and token_input.get("value"):
        return str(token_input["value"])
    return None


def parse_event_datetime(text: str) -> datetime | None:
    """Parse 'MM/DD/YYYY H:MM AM/PM' into a datetime."""
    m = _EVENT_DATETIME_RE.search(text)
    if not m:
        return None
    month = int(m.group("month"))
    day = int(m.group("day"))
    year = int(m.group("year"))
    hour = int(m.group("hour"))
    minute = int(m.group("minute"))
    ampm = m.group("ampm").upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


class SearchResult:
    """A single row from the Ventura tentative rulings search results table."""

    __slots__ = (
        "case_number",
        "event_type",
        "event_datetime",
        "department",
        "doc_id",
    )

    def __init__(
        self,
        case_number: str,
        event_type: str,
        event_datetime: datetime | None,
        department: str,
        doc_id: str | None,
    ) -> None:
        self.case_number = case_number
        self.event_type = event_type
        self.event_datetime = event_datetime
        self.department = department
        self.doc_id = doc_id


def parse_results_table(html: str) -> list[SearchResult]:
    """Parse the results table from the search response HTML.

    Expected table structure:
        <table>
          <thead><tr><th>Case Number</th><th>Event Type</th>
            <th>Event Date/Time</th><th>Department</th><th>Document</th></tr></thead>
          <tbody>
            <tr><td>...</td><td>...</td><td>...</td><td>...</td><td><a href="...">View</a></td></tr>
          </tbody>
        </table>
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[SearchResult] = []

    table = soup.find("table")
    if not table:
        return results

    # Find tbody rows
    tbody = table.find("tbody")
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]  # skip header

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        case_number = cells[0].get_text(strip=True)
        event_type = cells[1].get_text(strip=True)
        event_datetime_str = cells[2].get_text(strip=True)
        department = cells[3].get_text(strip=True)

        # Extract document link
        doc_link = cells[4].find("a", href=True)
        doc_id: str | None = None
        if doc_link:
            href = doc_link["href"]
            m = _VIEWFILE_RE.search(href)
            if m:
                doc_id = m.group(1)

        event_dt = parse_event_datetime(event_datetime_str)

        if not case_number:
            continue

        results.append(
            SearchResult(
                case_number=case_number,
                event_type=event_type,
                event_datetime=event_dt,
                department=department,
                doc_id=doc_id,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class VenturaTentativeRulingsScraper(BaseScraper):
    """Ventura County tentative rulings — search-based scraper.

    Unlike PDF-link scrapers, this scraper:
    1. GETs the search page to establish a session and extract the anti-forgery token
    2. POSTs a date-based search to get a results table
    3. Parses structured fields directly from the HTML table (no PDF parsing needed)
    4. Downloads individual ruling documents via ViewFile endpoints

    When a ``dept_judge_map`` or ``court_directory`` is provided, the scraper
    uses the department-to-judge mapping to populate judge names on each ruling.

    Parameters
    ----------
    config : ScraperConfig
        Scraper configuration.
    search_dates : list[datetime] | None
        Specific dates to search. If None, searches today's date.
    dept_judge_map : dict[str, str] | None
        Pre-fetched department-to-judge mapping. Used as a fallback when
        ``court_directory`` is not provided or for docs without hearing dates.
    court_directory : VenturaCourtDirectory | None
        Optional court directory instance for snapshotting and date-aware lookups.
    """

    def __init__(
        self,
        config: ScraperConfig,
        search_dates: list[datetime] | None = None,
        dept_judge_map: dict[str, str] | None = None,
        court_directory: object | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        self._search_dates = search_dates
        self._dept_judge_map: dict[str, str] = dept_judge_map or {}
        self._court_directory = court_directory
        self._court_id: str = "ca_ventura"

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch tentative rulings by searching for each configured date."""
        # If a court directory is provided, snapshot it and use the result
        # as the department-to-judge mapping for this scraper run.
        if self._court_directory is not None:
            from courts.ca.ventura_dept_judges import VenturaCourtDirectory

            self._court_id = (
                self._court_directory.COURT_ID
                if isinstance(self._court_directory, VenturaCourtDirectory)
                else "ca_ventura"
            )
            self._dept_judge_map = self._court_directory.fetch_and_snapshot(self._court_id)
            self._log.info(
                "Snapshotted court directory",
                court_id=self._court_id,
                departments=len(self._dept_judge_map),
            )

        docs: list[CapturedDocument] = []

        search_dates = self._search_dates or [
            datetime.now(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
        ]

        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            # Step 1: GET search page to establish session + get token
            self._log.info("Fetching search page", url=SEARCH_URL)
            response = client.get(SEARCH_URL)
            response.raise_for_status()

            token = extract_verification_token(response.text)
            if not token:
                raise RuntimeError("Failed to extract __RequestVerificationToken from search page")

            self._log.info("Extracted anti-forgery token")

            # Step 2: Search for each date
            for search_date in search_dates:
                time.sleep(self.config.request_delay_seconds)
                date_str = search_date.strftime("%-m/%-d/%Y")

                self._log.info("Searching for date", date=date_str)

                try:
                    date_docs = self._search_date(client, token, date_str, search_date)
                    docs.extend(date_docs)
                except Exception as exc:
                    self._log.error(
                        "Failed to search date",
                        date=date_str,
                        error=str(exc),
                    )

        return docs

    def _search_date(
        self,
        client: httpx.Client,
        token: str,
        date_str: str,
        search_date: datetime,
    ) -> list[CapturedDocument]:
        """POST a date search and process the results."""
        form_data = {
            "__RequestVerificationToken": token,
            "SearchFromDate": date_str,
        }
        response = client.post(SEARCH_URL, data=form_data)
        response.raise_for_status()

        results = parse_results_table(response.text)
        self._log.info("Found results", date=date_str, count=len(results))

        docs: list[CapturedDocument] = []
        for result in results:
            time.sleep(self.config.request_delay_seconds)
            try:
                doc = self._fetch_result_document(client, result, search_date)
                docs.append(doc)
            except Exception as exc:
                self._log.error(
                    "Failed to fetch document",
                    case_number=result.case_number,
                    doc_id=result.doc_id,
                    error=str(exc),
                )

        return docs

    def _fetch_result_document(
        self,
        client: httpx.Client,
        result: SearchResult,
        search_date: datetime,
    ) -> CapturedDocument:
        """Download a ruling document and create a CapturedDocument."""
        # Build document URL
        if result.doc_id:
            doc_url = f"{BASE_URL}/CaseInquiry/ViewFile/{result.doc_id}"
            response = client.get(doc_url)
            response.raise_for_status()
            raw_content = response.content

            # Determine content format from response headers
            content_type = response.headers.get("content-type", "")
            if "pdf" in content_type.lower():
                content_format = ContentFormat.PDF
            elif "html" in content_type.lower():
                content_format = ContentFormat.HTML
            else:
                content_format = ContentFormat.HTML  # default assumption
        else:
            # No document link — create a placeholder with table metadata
            doc_url = SEARCH_URL
            raw_content = b""
            content_format = ContentFormat.HTML

        doc = self._make_base_doc(
            source_url=doc_url,
            raw_content=raw_content,
            content_format=content_format,
        )

        # Populate structured fields from the table (pre-parsed by the court site)
        doc.case_number = result.case_number
        doc.motion_type = result.event_type if result.event_type else None
        doc.department = result.department if result.department else None
        doc.hearing_date = result.event_datetime or search_date

        # Store extra metadata
        doc.extra["doc_id"] = result.doc_id
        doc.extra["search_date"] = search_date.isoformat()
        doc.extra["event_type"] = result.event_type

        return doc

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Parse additional fields from the downloaded document content.

        Most structured fields are already populated from the search results table.
        This method attempts to extract ruling text, outcome, and case title from
        the document content itself.  It also uses the department-to-judge mapping
        (from ``dept_judge_map`` or ``court_directory``) to populate judge_name
        when it is not already set.
        """
        if doc.raw_content:
            try:
                if doc.content_format == ContentFormat.PDF:
                    text = _extract_pdf_text(doc.raw_content)
                else:
                    text = _extract_html_text(doc.raw_content)

                if text:
                    doc.ruling_text = text
                    if not doc.outcome:
                        doc.outcome = _extract_outcome(text)
                    if not doc.case_title:
                        doc.case_title = _extract_case_title(text)
            except Exception as exc:
                self._log.warning(
                    "Document parse error",
                    case_number=doc.case_number,
                    error=str(exc),
                )

        # Use department-to-judge mapping as fallback for judge_name.
        # When a court directory is available and the doc has a hearing
        # date, use the historical snapshot closest to that date so that
        # judge-to-department assignments are accurate for past rulings.
        if doc.judge_name is None and doc.department:
            from courts.ca.ventura_dept_judges import lookup_judge_for_department

            effective_map = self._dept_judge_map
            if self._court_directory is not None and doc.hearing_date is not None:
                date_map = self._court_directory.get_mapping_for_date(
                    self._court_id,
                    doc.hearing_date,
                    fallback=self._dept_judge_map,
                )
                if date_map is not None:
                    effective_map = date_map

            if effective_map:
                mapped_name = lookup_judge_for_department(effective_map, doc.department)
                if mapped_name:
                    doc.judge_name = mapped_name
                    self._log.debug(
                        "Judge name populated from department mapping",
                        department=doc.department,
                        judge_name=mapped_name,
                    )

        return doc


# ---------------------------------------------------------------------------
# Document text extraction helpers
# ---------------------------------------------------------------------------


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract full text from a PDF using pdfplumber."""
    import io

    import pdfplumber

    lines: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                lines.append(text)
    return "\n".join(lines)


def _extract_html_text(html_bytes: bytes) -> str:
    """Extract text from HTML ruling content."""
    try:
        html_str = html_bytes.decode("utf-8")
    except UnicodeDecodeError:
        html_str = html_bytes.decode("latin-1")

    soup = BeautifulSoup(html_str, "lxml")
    # Remove script and style elements
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return text


# Outcome patterns
_OUTCOME_RE = re.compile(
    r"\b(?P<outcome>"
    r"GRANTED|DENIED|SUSTAINED|OVERRULED|MOOT"
    r"|OFF\s+CALENDAR"
    r"|[Gg]ranted|[Dd]enied|[Ss]ustained|[Oo]verruled"
    r"|[Cc]ontinued"
    r")\b",
)


def _extract_outcome(text: str) -> str | None:
    """Extract ruling outcome from document text."""
    m = _OUTCOME_RE.search(text)
    if not m:
        return None
    raw = m.group("outcome").strip()
    # Normalize
    lower = raw.lower()
    if "granted" in lower:
        return "Granted"
    if "denied" in lower:
        return "Denied"
    if "sustained" in lower:
        return "Sustained"
    if "overruled" in lower:
        return "Overruled"
    if "off" in lower and "calendar" in lower:
        return "Off Calendar"
    if "moot" in lower:
        return "Moot"
    if "continued" in lower:
        return "Continued"
    return raw


# Case title patterns: "Party A v. Party B" or "In re: Something"
_CASE_TITLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Re:\s+)?(?P<title>[A-Z][^\n]{3,}?\s+v[s]?\.?\s+[A-Z][^\n]{3,})",
    re.MULTILINE,
)

_IN_RE_RE = re.compile(
    r"(?:^|\n)\s*(?:In\s+[Rr]e:\s+)(?P<title>[^\n]+)",
    re.MULTILINE,
)


def _extract_case_title(text: str) -> str | None:
    """Extract case title from document text."""
    # Try "Party v. Party" pattern first
    m = _CASE_TITLE_RE.search(text[:2000])
    if m:
        title = " ".join(m.group("title").strip().split())
        if len(title) > 200:
            title = title[:200]
        return title

    # Try "In re:" pattern
    m2 = _IN_RE_RE.search(text[:2000])
    if m2:
        title = " ".join(m2.group("title").strip().split())
        if len(title) > 200:
            title = title[:200]
        return title

    return None


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default Ventura tentative rulings scraper configuration."""
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-ventura-tentatives",
        state="CA",
        county="Ventura",
        court="Superior Court",
        target_urls=[SEARCH_URL],
        poll_interval_seconds=43200,  # twice daily
        schedule_windows=[
            ScheduleWindow(start=dtime(15, 0), end=dtime(16, 0)),  # 3 PM sweep
            ScheduleWindow(start=dtime(21, 0), end=dtime(22, 0)),  # 9 PM catch-up
        ],
        request_delay_seconds=1.0,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
