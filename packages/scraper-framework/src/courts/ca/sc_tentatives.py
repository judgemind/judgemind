"""Santa Clara County Superior Court — Civil Tentative Rulings Scraper (Pattern 5).

Pattern 5: Per-Department Web Pages with Document Links.

Verified against live site 2026-03-07:
  Landing:  https://santaclara.courts.ca.gov/online-services/tentative-rulings
  10 departments currently posting: 1, 2, 6, 7, 10, 12, 13, 16, 19, 22
  All located at Downtown Superior Court (DTS), 191 North First Street, San Jose

Navigation:
  1. Landing page lists department links (e.g. "Dept. 1") and judge name links
     (e.g. "Eunice W. Lee") — both point to the same department page URL.
  2. Each department page has 1-2 PDF links (one per hearing day, e.g. Tuesday/Thursday).
  3. PDFs contain full tentative rulings with headers, case numbers, and ruling text.

Department page URL patterns (not fully consistent):
  /online-services/tentative-rulings/department-N-tentative-rulings  (depts 1,2,6,7,10,12,13)
  /online-services/tentative-rulings/dept-N-tentative-rulings        (depts 16,19,22)

PDF URL pattern:
  /system/files/tentative-ruling/dept-N-day[_suffix].pdf

PDF structure (all departments):
  Header:  "SUPERIOR COURT, STATE OF CALIFORNIA"
           "COUNTY OF SANTA CLARA"
           "Department N"
           "Honorable Firstname Lastname, Presiding"
  Date:    "DATE: Month DD, YYYY" or "Month DD, YYYY" (standalone line)
  Cases:   "LINE N  CASENO  CaseTitle  MotionType" followed by ruling text
  Case numbers: DD{CV,PR}DDDDDD format (e.g. 24CV443183, 25PR199782)

Judge-to-department mapping is extracted from the landing page, where both
"Dept. N" and "Judge Name" links share the same URL.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urljoin

import httpx
import pdfplumber
import structlog
from bs4 import BeautifulSoup

from framework import (
    BaseScraper,
    CapturedDocument,
    ContentFormat,
    CourtDirectory,
    ScheduleWindow,
    ScraperConfig,
)

logger = structlog.get_logger(__name__)

LANDING_URL = "https://santaclara.courts.ca.gov/online-services/tentative-rulings"
BASE_URL = "https://santaclara.courts.ca.gov"
COURTHOUSE = "Downtown Superior Court"

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Department link text on landing page: "Dept. 1", "Dept. 22"
_DEPT_LINK_RE = re.compile(r"^Dept\.\s*(?P<department>\d+)$")

# Judge name from PDF header: "Honorable Eunice Lee, Presiding" or
# "Honorable Rafael Sivilla-Jones, Presiding"
_JUDGE_RE = re.compile(
    r"Honorable\s+(?P<judge_name>[A-Z][^\n,]+?),?\s+Presiding",
    re.IGNORECASE,
)

# Department number from PDF header: "Department 1", "Department 16"
_DEPT_PDF_RE = re.compile(r"^Department\s+(?P<department>\d+)$", re.MULTILINE)

# Hearing date from PDF: "DATE: March 3, 2026" or standalone "March 3, 2026"
_DATE_RE = re.compile(
    r"(?:DATE:\s*)?(?P<date>"
    r"(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{1,2},?\s+\d{4})",
)

# Case number: 2-digit year prefix + CV or PR + 6 digits (e.g. 24CV443183, 25PR199782)
_CASE_NUMBER_RE = re.compile(r"\b\d{2}(?:CV|PR)\d{6}\b", re.IGNORECASE)

# Case line in summary table: "LINE N CASENO CaseTitle MotionType/TentativeRuling"
# Also handles lines like "9:00 24CV443183" and ";"-separated lines
_CASE_LINE_RE = re.compile(
    r"(?:LINE\s+)?(?:\d+[,;]?\s+)?(?P<case_number>\d{2}(?:CV|PR)\d{6})\s+"
    r"(?P<case_title>[^\n]+?)(?:\s{2,})(?P<motion_or_ruling>[^\n]+)",
    re.IGNORECASE,
)

# Outcome keywords
_OUTCOME_RE = re.compile(
    r"\b(?P<outcome>"
    r"GRANTED|DENIED|SUSTAINED|OVERRULED|MOOT"
    r"|OFF\s+(?:CALENDAR|calendar)"
    r"|off\s+calendar"
    r")\b",
    re.IGNORECASE,
)

# Map raw outcome keywords to valid ruling_outcome enum values (#2113).
# SUSTAINED/OVERRULED are demurrer outcomes that map to granted/denied.
_SC_OUTCOME_MAP: dict[str, str] = {
    "granted": "granted",
    "denied": "denied",
    "sustained": "granted",
    "overruled": "denied",
    "moot": "moot",
}

# Motion type keywords (from the case line or ruling text)
_MOTION_TYPE_RE = re.compile(
    r"\b(?P<motion_type>"
    r"Demurrer|Motion\s+to\s+(?:Compel|Dismiss|Strike|Quash|Stay|Vacate|Set\s+Aside)"
    r"|Summary\s+Judgment|Summary\s+Adjudication"
    r"|(?:Petition|Motion)\s+(?:to\s+Compel\s+)?(?:Arbitration|Writ\s+of\s+Attachment)"
    r"|Writ\s+of\s+Attachment"
    r"|(?:Temporary\s+)?Restraining\s+Order"
    r"|Preliminary\s+Injunction"
    r"|Compromise\s+of\s+Minor(?:'s)?\s+Claim"
    r"|(?:Hearing(?:\s+on)?:?\s+)?Compromise\s+of\s+Minor(?:'s)?\s+Claim"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class DepartmentInfo:
    """Information about a single department discovered from the landing page."""

    def __init__(self, department: str, page_url: str, judge_name: str | None = None) -> None:
        self.department = department
        self.page_url = page_url
        self.judge_name = judge_name


# ---------------------------------------------------------------------------
# Court directory snapshot
# ---------------------------------------------------------------------------

COURT_ID = "ca_santa_clara"


class SantaClaraCourtDirectory(CourtDirectory):
    """Snapshot the Santa Clara dept-to-judge mapping using CourtDirectory infrastructure.

    Fetches the tentative rulings landing page and extracts the department-to-judge
    mapping from the link structure. The raw HTML is archived to S3 and the parsed
    mapping is stored in the ``court_directory_snapshots`` DB table.
    """

    def fetch_current(self) -> tuple[bytes, dict[str, str]]:
        """Fetch the live landing page and extract the dept-to-judge mapping.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_html_bytes, {department_number: judge_name}).
        """
        with httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            response = client.get(LANDING_URL)
            response.raise_for_status()

        raw = response.content
        departments = extract_departments(response.text)
        mapping: dict[str, str] = {}
        for dept in departments:
            if dept.judge_name:
                mapping[dept.department] = dept.judge_name
        return raw, mapping


# ---------------------------------------------------------------------------
# Landing page parsing
# ---------------------------------------------------------------------------


def extract_departments(html: str, base_url: str = BASE_URL) -> list[DepartmentInfo]:
    """Parse the landing page to discover departments and their judge names.

    The landing page has two types of links per department:
    1. "Dept. N" link → department page URL
    2. "Judge Name" link → same department page URL

    We use the shared URL to associate judge names with departments.
    """
    soup = BeautifulSoup(html, "lxml")

    # Build URL→department mapping from "Dept. N" links
    url_to_dept: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).replace("\xa0", " ")
        m = _DEPT_LINK_RE.match(text)
        if m:
            dept = m.group("department")
            url = urljoin(base_url, a["href"])
            if url not in url_to_dept:
                url_to_dept[url] = dept

    # Build URL→judge_name mapping from non-dept links that share the same URLs
    url_to_judge: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).replace("\xa0", " ")
        url = urljoin(base_url, a["href"])
        if url in url_to_dept and not _DEPT_LINK_RE.match(text):
            # This is a judge name link
            if text and url not in url_to_judge:
                url_to_judge[url] = text

    # Combine into DepartmentInfo objects
    departments: list[DepartmentInfo] = []
    seen: set[str] = set()
    for url, dept in url_to_dept.items():
        if dept in seen:
            continue
        seen.add(dept)
        judge = url_to_judge.get(url)
        departments.append(DepartmentInfo(department=dept, page_url=url, judge_name=judge))

    return departments


def extract_pdf_links_from_dept_page(html: str, base_url: str = BASE_URL) -> list[tuple[str, str]]:
    """Extract ruling PDF links from a department page.

    Returns list of (absolute_url, link_text) for PDF links that are tentative
    rulings (excludes rule PDFs like civil_0.pdf, probate_1.pdf).
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" not in href.lower():
            continue
        abs_url = urljoin(base_url, href) if not href.startswith("http") else href
        # Skip non-ruling PDFs (court rules)
        if "/rules/" in abs_url.lower():
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        link_text = a.get_text(separator=" ", strip=True).replace("\xa0", " ")
        results.append((abs_url, link_text))

    return results


# ---------------------------------------------------------------------------
# PDF text extraction and parsing
# ---------------------------------------------------------------------------


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract full text from a PDF using pdfplumber."""
    import io

    lines: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                lines.append(text)
    return "\n".join(lines)


def parse_judge_name(text: str) -> str | None:
    """Extract judge name from PDF header text."""
    m = _JUDGE_RE.search(text)
    if m:
        return " ".join(m.group("judge_name").strip().split())
    return None


def parse_department(text: str) -> str | None:
    """Extract department number from PDF header text."""
    m = _DEPT_PDF_RE.search(text)
    if m:
        return m.group("department")
    return None


def parse_hearing_date(text: str) -> Any:
    """Extract the first hearing date from PDF text.

    Returns a datetime object or None.
    """
    from datetime import datetime

    m = _DATE_RE.search(text)
    if not m:
        return None
    raw = " ".join(m.group("date").split())
    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_case_number(text: str) -> str | None:
    """Extract the first case number from PDF text.

    The match is case-insensitive; the returned value is always uppercase.
    """
    m = _CASE_NUMBER_RE.search(text)
    return m.group(0).upper() if m else None


def parse_all_case_numbers(text: str) -> list[str]:
    """Extract all unique case numbers from PDF text.

    The match is case-insensitive; returned values are always uppercase.
    """
    return list(dict.fromkeys(cn.upper() for cn in _CASE_NUMBER_RE.findall(text)))


def parse_outcome(text: str) -> str | None:
    """Extract the primary outcome from ruling text.

    Returns a value compatible with the ``ruling_outcome`` PostgreSQL enum
    (lowercase snake_case).  The ingestion worker normalizes outcomes via
    ``normalize_outcome()``, but returning DB-compatible values here avoids
    relying on downstream normalization and prevents DB write failures if
    the normalization step is accidentally skipped (see #2113).
    """
    m = _OUTCOME_RE.search(text)
    if m:
        raw = m.group("outcome").strip().lower()
        # Normalize "off calendar" variants
        if raw.startswith("off"):
            return "off_calendar"
        return _SC_OUTCOME_MAP.get(raw)
    return None


def parse_motion_type(text: str) -> str | None:
    """Extract the motion type from ruling text."""
    m = _MOTION_TYPE_RE.search(text)
    if m:
        raw = m.group("motion_type").strip()
        # Normalize whitespace and title-case
        normalized = " ".join(raw.split())
        # Title-case the first letter of each word, but preserve existing caps
        # (e.g. "Summary Judgment" stays as-is, "demurrer" → "Demurrer")
        if normalized and normalized[0].islower():
            normalized = normalized[0].upper() + normalized[1:]
        return normalized
    return None


def parse_case_title(text: str) -> str | None:
    """Extract case title (party names) from ruling text.

    Tries multiple strategies in order of specificity:
    1. "Case Name: ..." or "Case Title: ..." (Dept 7 probate format)
    2. "LINE N - CASENO – Title" (Dept 2 probate body format)
    3. Structured LINE table entry (civil calendar format)
    4. Fallback: "Party v. Party" pattern (excluding legal citations)
    """
    # Strategy 1: "Case Name:" or "Case Title:" field (Dept 7 probate format)
    case_name_match = re.search(
        r"Case\s+(?:Name|Title)\s*:\s*(?P<title>[^\n]{5,150})",
        text,
        re.IGNORECASE,
    )
    if case_name_match:
        title = " ".join(case_name_match.group("title").strip().split())
        if title and len(title) > 3:
            return title

    # Strategy 2: "LINE N - CASENO – Title" (appears in ruling body, not the
    # summary table).  This handles the Dept 2 probate format where the case
    # line in the body uses dashes/en-dashes as separators.  The dash between
    # LINE N and the case number is required to distinguish from the summary
    # table format (which uses spaces only).
    line_entry_match = re.search(
        r"LINE\s+\d+\s*[-–]\s*\d{2}(?:CV|PR)\d{6}\s*[-–]\s*(?P<title>[^\n]+)",
        text,
        re.IGNORECASE,
    )
    if line_entry_match:
        title = " ".join(line_entry_match.group("title").strip().split())
        if title and len(title) > 3:
            return title

    # Strategy 3: Structured LINE table: "LINE N CASENO CaseTitle  MotionOrRuling"
    m = _CASE_LINE_RE.search(text)
    if m:
        title = m.group("case_title").strip()
        title = " ".join(title.split())
        if title and len(title) > 3:
            return title

    # Strategy 4: Fallback "Party v. Party" pattern.
    # Exclude matches that start with procedural/legal-citation words like
    # "Petitioner", "Plaintiff", "Defendant" etc. — those indicate ruling
    # body text referencing case law, not the case title.
    citation_starters = re.compile(
        r"^(?:Petitioner|Plaintiff|Defendant|Respondent|Movant|The\s+Court"
        r"|See\s+|cf\.\s+|In\s+re\s+the\s+(?:Matter|Application))",
        re.IGNORECASE,
    )
    vs_re = re.compile(
        r"(?:^|\n)\s*(?P<title>[A-Z][^\n]{3,}?\s+v[s]?\.?\s+[A-Z][^\n]{3,})",
        re.MULTILINE,
    )
    for m2 in vs_re.finditer(text):
        candidate = " ".join(m2.group("title").strip().split())
        # Skip legal citations that match "v." but aren't the case title
        if citation_starters.match(candidate):
            continue
        if len(candidate) > 200:
            candidate = candidate[:200]
        return candidate

    return None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class SCTentativeRulingsScraper(BaseScraper):
    """Santa Clara County civil tentative rulings — Pattern 5.

    Two-level navigation:
    1. Landing page → discover department links and judge names
    2. Department pages → discover PDF links (1-2 per department, by hearing day)
    3. Download each PDF → parse rulings

    Parameters
    ----------
    config : ScraperConfig
        Scraper configuration.
    court_directory : SantaClaraCourtDirectory | None
        Optional court directory instance. When provided, the scraper will
        snapshot the department-to-judge mapping on each run via
        ``fetch_and_snapshot()``. When ``None``, the scraper falls back to
        fetching and parsing the landing page inline (no snapshot).
    """

    def __init__(
        self,
        config: ScraperConfig,
        court_directory: SantaClaraCourtDirectory | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        self._court_directory = court_directory
        self._dir_mapping: dict[str, str] = {}

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch all ruling PDFs from all departments."""
        docs: list[CapturedDocument] = []

        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            # Step 1: Fetch landing page and discover departments.
            # When a court directory is provided, snapshot the mapping and
            # still fetch the landing page to discover department URLs.
            if self._court_directory is not None:
                self._log.info("Snapshotting court directory", court_id=COURT_ID)
                self._dir_mapping = self._court_directory.fetch_and_snapshot(COURT_ID)
                self._log.info(
                    "Court directory snapshot taken",
                    court_id=COURT_ID,
                    departments=len(self._dir_mapping),
                )

            self._log.info("Fetching landing page", url=LANDING_URL)
            response = client.get(LANDING_URL)
            response.raise_for_status()

            departments = extract_departments(response.text)

            # If we have a directory mapping, enrich departments with judge names
            # from the snapshot (in case inline extraction missed any).
            if self._court_directory is not None:
                for dept_info in departments:
                    if not dept_info.judge_name and dept_info.department in self._dir_mapping:
                        dept_info.judge_name = self._dir_mapping[dept_info.department]

            self._log.info("Found departments", count=len(departments))

            # Step 2: For each department, fetch the page and find PDF links
            for dept_info in departments:
                time.sleep(self.config.request_delay_seconds)
                try:
                    dept_docs = self._fetch_department(client, dept_info)
                    docs.extend(dept_docs)
                except Exception as exc:
                    self._log.error(
                        "Failed to fetch department",
                        department=dept_info.department,
                        url=dept_info.page_url,
                        error=str(exc),
                    )

        return docs

    def _fetch_department(
        self, client: httpx.Client, dept_info: DepartmentInfo
    ) -> list[CapturedDocument]:
        """Fetch a department page and download all ruling PDFs."""
        self._log.debug(
            "Fetching department page",
            department=dept_info.department,
            url=dept_info.page_url,
        )
        response = client.get(dept_info.page_url)
        response.raise_for_status()

        pdf_links = extract_pdf_links_from_dept_page(response.text)
        self._log.debug(
            "Found PDF links",
            department=dept_info.department,
            count=len(pdf_links),
        )

        docs: list[CapturedDocument] = []
        for href, link_text in pdf_links:
            time.sleep(self.config.request_delay_seconds)
            try:
                doc = self._fetch_one_pdf(client, href, link_text, dept_info)
                docs.append(doc)
                self._log.debug(
                    "Fetched PDF",
                    department=doc.department,
                    judge=doc.judge_name,
                    url=href,
                )
            except Exception as exc:
                self._log.error(
                    "Failed to fetch PDF",
                    department=dept_info.department,
                    url=href,
                    error=str(exc),
                )

        return docs

    def _fetch_one_pdf(
        self,
        client: httpx.Client,
        href: str,
        link_text: str,
        dept_info: DepartmentInfo,
    ) -> CapturedDocument:
        """Download a single PDF and create a CapturedDocument."""
        response = client.get(href)
        response.raise_for_status()

        doc = self._make_base_doc(
            source_url=href,
            raw_content=response.content,
            content_format=ContentFormat.PDF,
        )
        doc.department = dept_info.department
        doc.judge_name = dept_info.judge_name
        doc.courthouse = COURTHOUSE
        doc.extra["link_text"] = link_text
        doc.extra["dept_page_url"] = dept_info.page_url
        return doc

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract structured fields from PDF text."""
        try:
            text = extract_pdf_text(doc.raw_content)
            doc.ruling_text = text

            # Extract case numbers
            case_numbers = parse_all_case_numbers(text)
            if case_numbers:
                doc.case_number = case_numbers[0]
                if len(case_numbers) > 1:
                    doc.extra["all_case_numbers"] = case_numbers

            # Extract hearing date
            if not doc.hearing_date:
                doc.hearing_date = parse_hearing_date(text)

            # Refine judge name from PDF text if not set from landing page
            pdf_judge = parse_judge_name(text)
            if pdf_judge:
                doc.judge_name = pdf_judge

            # Refine department from PDF text if not set
            pdf_dept = parse_department(text)
            if pdf_dept and not doc.department:
                doc.department = pdf_dept

            # Extract case title from first case in the PDF
            case_title = parse_case_title(text)
            if case_title:
                doc.case_title = case_title

            # Extract motion type and outcome from the ruling text
            motion = parse_motion_type(text)
            if motion:
                doc.motion_type = motion

            outcome = parse_outcome(text)
            if outcome:
                doc.outcome = outcome

        except Exception as exc:
            self._log.warning("PDF parse error", error=str(exc))

        # Fallback: if judge name is still missing after PDF extraction,
        # use the date-appropriate directory snapshot for the hearing date.
        if doc.judge_name is None and doc.department and self._court_directory is not None:
            effective_map = self._dir_mapping
            if doc.hearing_date is not None:
                date_map = self._court_directory.get_mapping_for_date(
                    COURT_ID,
                    doc.hearing_date,
                    fallback=self._dir_mapping,
                )
                if date_map is not None:
                    effective_map = date_map
            judge_name = effective_map.get(doc.department)
            if judge_name:
                doc.judge_name = judge_name
                self._log.debug(
                    "Judge name populated from directory snapshot",
                    department=doc.department,
                    judge_name=judge_name,
                )

        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Create the default scraper configuration for Santa Clara County."""
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-sc-tentatives-civil",
        state="CA",
        county="Santa Clara",
        court="Superior Court",
        target_urls=[LANDING_URL],
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
