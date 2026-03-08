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
  Case numbers: DDCVDDDDDD format (e.g. 24CV443183, 25CV460465)

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

from framework import BaseScraper, CapturedDocument, ContentFormat, ScheduleWindow, ScraperConfig

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

# Case number: 2-digit year prefix + CV + 6 digits (e.g. 24CV443183, 25CV460465)
_CASE_NUMBER_RE = re.compile(r"\b\d{2}CV\d{6}\b")

# Case line in summary table: "LINE N CASENO CaseTitle MotionType/TentativeRuling"
# Also handles lines like "9:00 24CV443183" and ";"-separated lines
_CASE_LINE_RE = re.compile(
    r"(?:LINE\s+)?(?:\d+[,;]?\s+)?(?P<case_number>\d{2}CV\d{6})\s+"
    r"(?P<case_title>[^\n]+?)(?:\s{2,})(?P<motion_or_ruling>[^\n]+)",
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
    """Extract the first case number from PDF text."""
    m = _CASE_NUMBER_RE.search(text)
    return m.group(0) if m else None


def parse_all_case_numbers(text: str) -> list[str]:
    """Extract all unique case numbers from PDF text."""
    return list(dict.fromkeys(_CASE_NUMBER_RE.findall(text)))


def parse_outcome(text: str) -> str | None:
    """Extract the primary outcome from ruling text."""
    m = _OUTCOME_RE.search(text)
    if m:
        raw = m.group("outcome").strip()
        # Normalize "off calendar" variants
        if raw.lower().startswith("off"):
            return "Off calendar"
        return raw.upper()
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

    Looks for patterns like "CaseName vs CaseName" or structured case lines.
    """
    # Try the structured LINE format first: "LINE N CASENO CaseTitle MotionType"
    m = _CASE_LINE_RE.search(text)
    if m:
        title = m.group("case_title").strip()
        # Clean up extra whitespace
        title = " ".join(title.split())
        if title and len(title) > 3:
            return title

    # Fallback: look for "Plaintiff, ... vs. Defendant, ..."
    vs_re = re.compile(
        r"(?:^|\n)\s*(?P<title>[A-Z][^\n]{3,}?\s+v[s]?\.?\s+[A-Z][^\n]{3,})",
        re.MULTILINE,
    )
    m2 = vs_re.search(text)
    if m2:
        title = " ".join(m2.group("title").strip().split())
        # Truncate at reasonable length
        if len(title) > 200:
            title = title[:200]
        return title

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
    """

    def __init__(self, config: ScraperConfig, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch all ruling PDFs from all departments."""
        docs: list[CapturedDocument] = []

        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            # Step 1: Fetch landing page and discover departments
            self._log.info("Fetching landing page", url=LANDING_URL)
            response = client.get(LANDING_URL)
            response.raise_for_status()

            departments = extract_departments(response.text)
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
