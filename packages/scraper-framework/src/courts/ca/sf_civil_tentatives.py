"""San Francisco Superior Court — Civil Tentative Rulings Scraper.

Scrapes civil tentative rulings from the SF court's tr.dll AJAX API.
This is separate from the family law scraper (sf_tentatives.py) which
uses the ufctr.dll endpoint.

Data source:
  POST https://webapps.sftc.org/tr/tr.dll
  Content-Type: application/x-www-form-urlencoded
  Body: RulingID=<N>&SearchBy=CourtDate&DatePick=<MM/DD/YYYY>&CaseNum=

Response: {"result": [<count>, "<html_table>"]}

The HTML table contains structured <tr> rows with headers:
  Case Number, Case Title, Court Date, Calendar Matter, Rulings

7 RulingIDs map to 5 departments:
  10 → Dept 301 (Law & Motion/Discovery, odd case numbers)
   2 → Dept 302 (Law & Motion/Discovery, even case numbers)
   6 → Dept 304 (Asbestos Discovery)
   5 → Dept 304 (Asbestos Law & Motion)
   8 → Dept 304 (Asbestos Motion Calendar)
   9 → Dept 210 (Real Property Housing Court Motions)
   3 → Dept 501 (Real Property Housing Court Motions)

The site is behind Cloudflare Turnstile CAPTCHA. If blocked (302
redirect to captcha.dll), the scraper logs a warning and returns
empty results. A future task will address CAPTCHA bypass.

No LLM extraction needed — all fields are deterministically parseable
from the structured HTML response.

Investigation: #2129
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from bs4 import BeautifulSoup

from framework import BaseScraper, CapturedDocument, ContentFormat, ScheduleWindow, ScraperConfig
from framework.events import EventBus
from framework.storage import S3Archiver

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CIVIL_API_URL = "https://webapps.sftc.org/tr/tr.dll"

# RulingID → department mapping.
# Each entry defines the department and a description of the motion types.
RULING_ID_MAP: dict[int, dict[str, str]] = {
    10: {"department": "301", "type": "Law & Motion/Discovery (odd)"},
    2: {"department": "302", "type": "Law & Motion/Discovery (even)"},
    6: {"department": "304", "type": "Asbestos Discovery"},
    5: {"department": "304", "type": "Asbestos Law & Motion"},
    8: {"department": "304", "type": "Asbestos Motion Calendar"},
    9: {"department": "210", "type": "Real Property Housing Court Motions"},
    3: {"department": "501", "type": "Real Property Housing Court Motions"},
}

# Ordered list of RulingIDs to fetch.
RULING_IDS: list[int] = sorted(RULING_ID_MAP.keys())

# Civil case number pattern: 3 uppercase letters + 2-digit year + 6 digits.
# e.g., CPF23518377, CGC22602981
_CASE_NUMBER_RE = re.compile(r"\b[A-Z]{3}\d{8}\b")

# Outcome patterns from ruling text (lowercase DB enum values).
_OUTCOME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bis\s+granted\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bis\s+denied\b", re.IGNORECASE), "denied"),
    (re.compile(r"\bis\s+sustained\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bis\s+overruled\b", re.IGNORECASE), "denied"),
    (re.compile(r"\bis\s+continued\b", re.IGNORECASE), "continued"),
    (re.compile(r"\bis\s+moot\b", re.IGNORECASE), "moot"),
    (re.compile(r"\bis\s+vacated\b", re.IGNORECASE), "off_calendar"),
    (re.compile(r"\btaken off calendar\b", re.IGNORECASE), "off_calendar"),
]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


@dataclass
class ParsedRuling:
    """A single ruling parsed from the tr.dll AJAX response."""

    case_number: str | None
    case_title: str | None
    court_date: str | None
    calendar_matter: str | None
    ruling_text: str | None
    ruling_html: str | None
    case_info_url: str | None


def parse_api_response(json_text: str) -> list[ParsedRuling]:
    """Parse the tr.dll AJAX response into individual rulings.

    The response is JSON: {"result": [count, "<html_table>"]}.
    The HTML table contains <tr> rows grouped by ruling, each with
    fields identified by <td class="dataHeader"> cells.

    Args:
        json_text: The raw JSON response from tr.dll.

    Returns:
        A list of ParsedRuling objects, one per ruling in the response.
    """
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("sf_civil.invalid_json")
        return []

    result = data.get("result")
    if not isinstance(result, list) or len(result) < 2:
        logger.warning("sf_civil.unexpected_result_shape", result_type=type(result).__name__)
        return []

    count = result[0]
    if not isinstance(count, int) or count == 0:
        return []

    html_str = result[1]
    if not isinstance(html_str, str) or not html_str.strip():
        return []

    return _parse_rulings_html(html_str)


def _parse_rulings_html(html_str: str) -> list[ParsedRuling]:
    """Parse the HTML table fragment into individual rulings.

    Each ruling is a group of <tr> rows. The first row of each ruling
    has a <td class="dataHeader"> with text "Case Number:".

    Args:
        html_str: The HTML table fragment from the API response.

    Returns:
        A list of ParsedRuling objects.
    """
    soup = BeautifulSoup(html_str, "html.parser")
    rows = soup.find_all("tr")

    # Group rows by ruling — each ruling starts with "Case Number:".
    # Skip any leading rows before the first "Case Number:" row
    # (e.g., header rows or blank rows injected by the server).
    ruling_groups: list[list[Any]] = []
    current_group: list[Any] = []
    found_first_ruling = False

    for row in rows:
        header = row.find("td", class_="dataHeader")
        is_ruling_start = header and header.get_text(strip=True) == "Case Number:"

        if is_ruling_start:
            if current_group:
                ruling_groups.append(current_group)
            current_group = [row]
            found_first_ruling = True
        elif found_first_ruling:
            current_group.append(row)

    if current_group:
        ruling_groups.append(current_group)

    rulings: list[ParsedRuling] = []
    for group in ruling_groups:
        ruling = _parse_ruling_group(group)
        rulings.append(ruling)

    return rulings


def _parse_ruling_group(rows: list[Any]) -> ParsedRuling:
    """Parse a group of <tr> rows into a single ParsedRuling.

    Each row has a <td class="dataHeader"> with the field name and
    the field value in the last <td>.

    Args:
        rows: The list of <tr> elements for a single ruling.

    Returns:
        A ParsedRuling with fields populated from the HTML.
    """
    fields: dict[str, Any] = {}

    for row in rows:
        header_td = row.find("td", class_="dataHeader")
        if not header_td:
            continue

        field_name = header_td.get_text(strip=True).rstrip(":")
        tds = row.find_all("td")
        if len(tds) < 2:
            continue

        value_td = tds[-1]
        fields[field_name] = value_td

    # Extract case number and case info URL from the link
    case_number: str | None = None
    case_info_url: str | None = None
    case_number_td = fields.get("Case Number")
    if case_number_td:
        link = case_number_td.find("a")
        if link:
            case_number = link.get_text(strip=True)
            case_info_url = link.get("href")
        else:
            case_number = case_number_td.get_text(strip=True)

    # Extract case title
    case_title: str | None = None
    case_title_td = fields.get("Case Title")
    if case_title_td:
        case_title = case_title_td.get_text(strip=True) or None

    # Extract court date
    court_date: str | None = None
    court_date_td = fields.get("Court Date")
    if court_date_td:
        court_date = court_date_td.get_text(strip=True) or None

    # Extract calendar matter (motion type)
    calendar_matter: str | None = None
    calendar_matter_td = fields.get("Calendar Matter")
    if calendar_matter_td:
        calendar_matter = calendar_matter_td.get_text(strip=True) or None

    # Extract ruling text (both plain text and HTML)
    ruling_text: str | None = None
    ruling_html: str | None = None
    ruling_td = fields.get("Rulings")
    if ruling_td:
        ruling_text = ruling_td.get_text(separator="\n", strip=True) or None
        # Get the inner HTML content
        ruling_html = "".join(str(child) for child in ruling_td.children).strip() or None

    return ParsedRuling(
        case_number=case_number,
        case_title=case_title,
        court_date=court_date,
        calendar_matter=calendar_matter,
        ruling_text=ruling_text,
        ruling_html=ruling_html,
        case_info_url=case_info_url,
    )


def parse_hearing_date(court_date_str: str | None) -> datetime | None:
    """Parse a hearing date from the Court Date field.

    Handles format: "2026-03-23 09:00 AM"

    Args:
        court_date_str: The raw court date string.

    Returns:
        A datetime object, or None if parsing fails.
    """
    if not court_date_str:
        return None
    # Try "YYYY-MM-DD HH:MM AM/PM" format
    try:
        return datetime.strptime(court_date_str.strip(), "%Y-%m-%d %I:%M %p")
    except ValueError:
        pass
    # Try "YYYY-MM-DD" only
    try:
        return datetime.strptime(court_date_str.strip()[:10], "%Y-%m-%d")
    except ValueError:
        pass
    # Try "MM/DD/YYYY" format
    try:
        return datetime.strptime(court_date_str.strip()[:10], "%m/%d/%Y")
    except ValueError:
        pass
    return None


def extract_outcome(ruling_text: str | None) -> str | None:
    """Extract the outcome from ruling text using keyword patterns.

    Args:
        ruling_text: The full ruling text.

    Returns:
        A lowercase outcome string (e.g., "granted", "denied"), or None.
    """
    if not ruling_text:
        return None
    for pattern, outcome in _OUTCOME_PATTERNS:
        if pattern.search(ruling_text):
            return outcome
    return None


def extract_parties_from_title(case_title: str | None) -> list[dict[str, str]]:
    """Extract plaintiff/petitioner and defendant/respondent from case title.

    The case title uses "VS." as separator. The part before VS. is the
    plaintiff/petitioner; the part after is the defendant/respondent.

    Handles titles with "ET AL" suffix and truncated titles.

    Args:
        case_title: The raw case title string.

    Returns:
        A list of party dicts with "name" and "role" keys.
    """
    if not case_title:
        return []

    # Split on " VS. " or " VS " (case-insensitive)
    parts = re.split(r"\s+VS\.?\s+", case_title, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) < 2:
        return []

    plaintiff_raw = parts[0].strip()
    defendant_raw = parts[1].strip()

    parties: list[dict[str, str]] = []

    if plaintiff_raw:
        plaintiff_name = _clean_party_name(plaintiff_raw)
        if plaintiff_name:
            parties.append({"name": plaintiff_name, "role": "plaintiff"})

    if defendant_raw:
        defendant_name = _clean_party_name(defendant_raw)
        if defendant_name:
            parties.append({"name": defendant_name, "role": "defendant"})

    return parties


def _clean_party_name(raw_name: str) -> str | None:
    """Clean a raw party name by removing trailing artifacts.

    Strips ", ET AL", "ET AL.", trailing commas, and extra whitespace.
    Title-cases ALL-CAPS names.

    Args:
        raw_name: The raw party name.

    Returns:
        The cleaned name, or None if the result is empty.
    """
    name = raw_name.strip()
    # Remove trailing "ET AL", "ET AL.", ", ET AL", ", ET AL."
    name = re.sub(r",?\s+ET\s+AL\.?\s*$", "", name, flags=re.IGNORECASE)
    # Remove trailing punctuation
    name = name.rstrip(",;. ")
    if not name:
        return None
    # Title-case if all uppercase
    if name.isupper():
        name = name.title()
    return name


def normalize_case_title(raw_title: str | None) -> str | None:
    """Normalize a case title for storage.

    Title-cases ALL-CAPS titles and cleans up whitespace.

    Args:
        raw_title: The raw case title from the API.

    Returns:
        The normalized title, or None.
    """
    if not raw_title:
        return None
    title = raw_title.strip()
    if not title:
        return None
    # Title-case if ALL CAPS
    if title.isupper():
        title = title.title()
    # Normalize whitespace
    title = " ".join(title.split())
    return title


# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------


class SFCivilTentativeRulingsScraper(BaseScraper):
    """San Francisco civil tentative rulings — AJAX API pattern.

    Fetches rulings from the tr.dll endpoint for all 7 RulingIDs
    (5 departments). Each ruling is emitted as a separate CapturedDocument.

    The site is behind Cloudflare Turnstile. If the API returns a redirect
    (302) to captcha.dll, the scraper logs a warning and skips that request.

    An optional ``dept_judge_map`` (department → judge name) can be passed
    to populate judge names from the SF court roster. When provided, the
    judge name is looked up by department number. When absent, the
    judge_name field is left empty (to be resolved downstream by enrichment).
    """

    def __init__(
        self,
        config: ScraperConfig,
        archiver: S3Archiver | None = None,
        event_bus: EventBus | None = None,
        dept_judge_map: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(config=config, archiver=archiver, event_bus=event_bus)
        self._dept_judge_map: dict[str, str] = dept_judge_map or {}

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch civil tentative rulings from the tr.dll AJAX API.

        For each RulingID, sends a POST request with today's date.
        Parses the JSON response and creates one CapturedDocument
        per ruling entry.

        Returns:
            A list of CapturedDocument objects, one per ruling.
        """
        docs: list[CapturedDocument] = []
        today = datetime.now(UTC)
        date_str = today.strftime("%m/%d/%Y")

        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=False,  # Detect CAPTCHA redirects
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            for ruling_id in RULING_IDS:
                time.sleep(self.config.request_delay_seconds)

                dept_info = RULING_ID_MAP[ruling_id]
                department = dept_info["department"]

                try:
                    response = client.post(
                        CIVIL_API_URL,
                        data={
                            "RulingID": str(ruling_id),
                            "SearchBy": "CourtDate",
                            "DatePick": date_str,
                            "CaseNum": "",
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )

                    # Check for CAPTCHA redirect
                    if response.status_code in (301, 302, 303, 307):
                        location = response.headers.get("location", "")
                        if "captcha" in location.lower():
                            self._log.warning(
                                "CAPTCHA redirect detected",
                                ruling_id=ruling_id,
                                department=department,
                                redirect_url=location,
                            )
                            continue
                        self._log.warning(
                            "Unexpected redirect",
                            ruling_id=ruling_id,
                            status=response.status_code,
                            location=location,
                        )
                        continue

                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    self._log.error(
                        "HTTP error fetching rulings",
                        ruling_id=ruling_id,
                        department=department,
                        status=exc.response.status_code,
                    )
                    continue
                except httpx.RequestError as exc:
                    self._log.error(
                        "Request error fetching rulings",
                        ruling_id=ruling_id,
                        department=department,
                        error=str(exc),
                    )
                    continue

                # Parse the response
                rulings = parse_api_response(response.text)
                if not rulings:
                    self._log.info(
                        "No rulings for RulingID",
                        ruling_id=ruling_id,
                        department=department,
                    )
                    continue

                self._log.info(
                    "Fetched rulings",
                    ruling_id=ruling_id,
                    department=department,
                    count=len(rulings),
                )

                # Create one CapturedDocument per ruling
                for ruling in rulings:
                    doc = self._ruling_to_document(
                        ruling=ruling,
                        ruling_id=ruling_id,
                        department=department,
                    )
                    docs.append(doc)

        return docs

    def _ruling_to_document(
        self,
        ruling: ParsedRuling,
        ruling_id: int,
        department: str,
    ) -> CapturedDocument:
        """Convert a ParsedRuling into a CapturedDocument.

        The raw_content is the per-ruling HTML text. Each ruling gets
        unique raw_content so that content-addressed archival (SHA-256 hash
        → document_id) produces a unique document per ruling. Archiving the
        full JSON response would cause all rulings from the same API call
        to share the same content_hash and collapse into one document.

        Args:
            ruling: The parsed ruling data.
            ruling_id: The RulingID used to fetch this ruling.
            department: The department number.

        Returns:
            A populated CapturedDocument.
        """
        # Use the ruling HTML as raw content for archival
        content = ruling.ruling_html or ruling.ruling_text or ""
        raw_content = content.encode("utf-8")

        source_url = f"{CIVIL_API_URL}?RulingID={ruling_id}"

        doc = self._make_base_doc(
            source_url=source_url,
            raw_content=raw_content,
            content_format=ContentFormat.HTML,
        )

        doc.department = department
        doc.courthouse = "San Francisco Courthouse"
        doc.case_number = ruling.case_number
        doc.case_title = normalize_case_title(ruling.case_title)
        doc.hearing_date = parse_hearing_date(ruling.court_date)
        doc.ruling_text = ruling.ruling_text
        doc.ruling_text_html = ruling.ruling_html
        doc.motion_type = ruling.calendar_matter
        doc.outcome = extract_outcome(ruling.ruling_text)
        doc.parties = extract_parties_from_title(ruling.case_title)

        # Resolve judge name from department via court roster
        doc.judge_name = self._dept_judge_map.get(department)

        # Store metadata for downstream processing
        doc.extra["ruling_id"] = ruling_id
        doc.extra["dept_type"] = RULING_ID_MAP[ruling_id]["type"]
        if ruling.case_info_url:
            doc.extra["case_info_url"] = ruling.case_info_url

        return doc

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Parse structured fields from a CapturedDocument.

        For this scraper, most fields are already populated in
        fetch_documents via _ruling_to_document. This method handles
        any additional parsing that couldn't be done during fetch.

        Args:
            doc: The document to parse.

        Returns:
            The document with any additional fields populated.
        """
        # Fields are pre-populated during fetch — no additional parsing needed
        # since the AJAX API returns structured data.
        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default SF civil scraper configuration.

    Args:
        s3_bucket: The S3 bucket for archival (empty string for local-only).

    Returns:
        A ScraperConfig for the SF civil tentative rulings scraper.
    """
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-sf-tentatives-civil",
        state="CA",
        county="San Francisco",
        court="Superior Court",
        target_urls=[CIVIL_API_URL],
        poll_interval_seconds=43200,  # twice daily
        schedule_windows=[
            ScheduleWindow(start=dtime(14, 0), end=dtime(15, 0)),  # 2 PM sweep
            ScheduleWindow(start=dtime(21, 0), end=dtime(22, 0)),  # 9 PM catch-up
        ],
        request_delay_seconds=1.0,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
