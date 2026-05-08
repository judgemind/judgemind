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
# Multi-ruling PDF splitter (#4303, format-B added in #4341)
# ---------------------------------------------------------------------------
#
# Santa Clara multi-case PDFs use one of two formats:
#
#   A) "Line N" expanded format (most departments — e.g. dept 16):
#        Each case starts on a line containing only ``Line N`` (case-
#        insensitive), followed by ``Case Name:`` and ``Case No.:`` headers
#        and the per-case ruling body.  Trailing ``Line N`` labels with no
#        body (e.g. cases that were taken off-calendar after the calendar
#        was published) appear at the end of the PDF — these are skipped
#        because their entry body is empty.
#
#   B) Compact summary-table format (e.g. dept 6):
#        A single ``LINE CASE NO. CASE TITLE TENTATIVE RULING`` header,
#        then per-row entries on shared rows.  This format does NOT have
#        per-case bare ``Line N`` boundaries on their own line for short
#        rulings — pdfplumber's flat text output collapses the table
#        columns and the format-A regex finds at most 1 entry.  Format B
#        is parsed by ``_split_rulings_format_b`` using pdfplumber's
#        ``extract_tables()`` API to read the structured columns
#        directly: every row of the summary table whose CASE NO. column
#        is non-empty becomes one SplitRuling, with ``case_number``,
#        ``case_title``, and ``ruling_text`` (cell + continuation rows)
#        populated deterministically from the table.  Format B requires
#        the original PDF bytes — text-only callers fall back to format A
#        for backward compatibility.  See #4341 (root cause #4339).
#
# The splitter handles both formats — format A is the dominant source of
# the ``all_same_case_title_cluster`` signal in Santa Clara per the
# cross-county audit (#4289 ran 2026-05-06; 21 distinct multi-case PDFs
# all produced the same wrong ``case_title`` across rulings) and format
# B drains the residual cluster of dept-6-style PDFs that the LLM-only
# path was hallucinating generic titles for.  Same carry-forward
# fingerprint #3534 (Fresno) and #3649 (Riverside) fixed by pre-LLM
# splitters.

# Entry boundary: a bare ``Line N`` label on its own line, case-insensitive.
# pdfplumber's text output puts each ``Line N`` on its own line in the
# Santa Clara expanded format — the splitter anchors on this.
# Negative lookahead for ``Line\s+\d+\s*$`` followed by another ``Line\s+\d+\s*$``
# (a trailing index label) is unnecessary because we filter on body length
# below.
_SC_RULING_ENTRY_RE = re.compile(
    r"^(?P<num>Line\s+\d{1,3})\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Within an entry, the structured per-case headers Santa Clara uses in the
# expanded format.  These are deterministic enough to extract case_number
# and case_title without involving the LLM.  Matching is case-insensitive.
_SC_CASE_NO_HEADER_RE = re.compile(
    r"^Case\s+No\.?:\s*(?P<case_number>\d{2}(?:CV|PR)\d{6})\s*$",
    re.MULTILINE | re.IGNORECASE,
)

_SC_CASE_NAME_HEADER_RE = re.compile(
    r"^Case\s+Name:\s*(?P<case_title>[^\n]+)$",
    re.MULTILINE | re.IGNORECASE,
)

# Body sanity threshold: a real ruling has at least this many characters of
# body text after the boundary.  Trailing index labels (``Line 10`` through
# ``Line 15`` at the end of a calendar PDF) have only single-digit page-
# footer noise after them — those are not rulings and must be skipped.
# 80 chars is comfortably below the smallest real ruling body in the
# fixtures (the smallest is ~430 chars for a "see Line N above" cross-
# reference entry, which is still legitimate calendar content) and well
# above the page-footer noise.
_SC_MIN_ENTRY_BODY_LEN = 80


class SplitRuling:
    """A single ruling extracted from a multi-ruling Santa Clara PDF.

    Mirrors ``courts.ca.riverside_tentatives.SplitRuling`` (#3649).  The
    splitter populates ``case_number`` and ``case_title`` deterministically
    from the per-entry ``Case No.:`` / ``Case Name:`` headers when present,
    and leaves ``motion_type`` / ``outcome`` ``None`` so per-entry LLM
    enrichment runs against only the entry's own text — eliminating the
    cross-entry carry-forward window (#4303).
    """

    __slots__ = (
        "ruling_index",
        "case_number",
        "ruling_text",
        "case_title",
        "motion_type",
        "outcome",
        "hearing_date",
        "department",
    )

    def __init__(
        self,
        ruling_index: int,
        case_number: str | None,
        ruling_text: str,
        case_title: str | None = None,
        motion_type: str | None = None,
        outcome: str | None = None,
        hearing_date: Any = None,
        department: str | None = None,
    ) -> None:
        self.ruling_index = ruling_index
        self.case_number = case_number
        self.ruling_text = ruling_text
        self.case_title = case_title
        self.motion_type = motion_type
        self.outcome = outcome
        self.hearing_date = hearing_date
        self.department = department


# Format-B summary-table header anchor (#4341).  pdfplumber's
# ``extract_tables()`` produces the structured table when this header is
# present on the page — see ``_split_rulings_format_b``.  The literal
# header in the live PDFs is ``LINE CASE NO. CASE TITLE TENTATIVE RULING``.
_SC_FORMAT_B_HEADER_RE = re.compile(
    r"LINE\s+CASE\s+NO\.?\s+CASE\s+TITLE\s+TENTATIVE\s+RULING",
    re.IGNORECASE,
)

# Format-B per-row case number column.  pdfplumber may emit the case
# number with internal whitespace (e.g. ``"24CV 443183"``) so we strip
# whitespace before matching.  ``\d{2}(CV|PR)\d{6}`` is the canonical SC
# case-number shape (see ``_CASE_NUMBER_RE``).
_SC_FORMAT_B_CASE_NO_RE = re.compile(
    r"^\s*(?P<case_number>\d{2}(?:CV|PR)\d{6})\s*$",
    re.IGNORECASE,
)

# Cross-reference inside a format-B summary-table cell:
# ``See Line N below`` / ``see Line N above`` / ``See Line Item N below``.
# When present, the format-B parser appends the corresponding format-A
# ``Line N`` body section (when one exists in the same PDF) to the row's
# ruling_text.  Only digits are captured.
_SC_FORMAT_B_CROSSREF_RE = re.compile(
    r"\bSee\s+Line(?:\s+Item)?\s+(?P<num>\d{1,3})\b",
    re.IGNORECASE,
)


def _normalize_table_cell(cell: str | None) -> str:
    """Collapse newlines + multi-whitespace inside a single pdfplumber cell."""
    if cell is None:
        return ""
    return " ".join(cell.replace("\n", " ").split())


def _is_format_b_header_row(row: list[str | None]) -> bool:
    """Return True if a pdfplumber-extracted table row is the format-B header.

    The first 4 cells must be (case-insensitive, leading-whitespace-tolerant)
    ``"LINE"``, ``"CASE NO."``, ``"CASE TITLE"``, ``"TENTATIVE RULING"``.
    Trailing cells (``None`` or empty) are ignored.
    """
    if len(row) < 4:
        return False
    expected = ("line", "case no.", "case title", "tentative ruling")
    for col, want in zip(row[:4], expected, strict=False):
        # Compare with normalized punctuation: pdfplumber may strip the
        # trailing period from "CASE NO."  Tolerate either form.
        normalized_loose = _normalize_table_cell(col).lower().rstrip(".")
        if normalized_loose != want.rstrip("."):
            return False
    return True


def _split_rulings_format_a(text: str) -> list[SplitRuling]:
    """Format-A splitter — the original ``Line N`` boundary regex (#4303).

    See module docstring §A for the exact pre-conditions.  Returns an
    empty list when no ``Line N`` boundaries are present (typical of
    compact summary-table PDFs and single-ruling PDFs).
    """
    matches = list(_SC_RULING_ENTRY_RE.finditer(text))
    if not matches:
        return []

    rulings: list[SplitRuling] = []
    for i, match in enumerate(matches):
        # ``Line N`` -> integer.
        num_str = match.group("num")
        digits = re.search(r"\d+", num_str)
        if not digits:
            continue
        try:
            entry_num = int(digits.group(0))
        except ValueError:
            continue

        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

        body = text[start:end].strip()
        if len(body) < _SC_MIN_ENTRY_BODY_LEN:
            # Trailing index label (e.g. dept 16 emits ``Line 10`` through
            # ``Line 15`` as bare labels at the end of the PDF for cases
            # that were dropped from the calendar after publication).
            # Skip — there is no ruling here.
            continue

        # Extract structured headers when present.  These are case-
        # insensitive and tolerant of the headers appearing anywhere in
        # the body (the dept 16 format has them as the first two lines
        # after ``Line N``; other departments may emit them later).
        case_no_match = _SC_CASE_NO_HEADER_RE.search(body)
        case_name_match = _SC_CASE_NAME_HEADER_RE.search(body)
        case_number = case_no_match.group("case_number").upper() if case_no_match else None
        case_title = (
            " ".join(case_name_match.group("case_title").split()) if case_name_match else None
        )

        rulings.append(
            SplitRuling(
                ruling_index=entry_num,
                case_number=case_number,
                ruling_text=body,
                case_title=case_title,
            )
        )

    return rulings


def _format_a_body_section(text: str, line_num: int) -> str | None:
    """Find the ``Line N`` body section in ``text`` and return its content.

    Used by the format-B parser to expand ``See Line N below`` cross-
    references in summary-table cells.  Returns ``None`` if no such
    section is present.

    The section starts at the boundary ``^Line\\s+N\\s*$`` (case-
    insensitive) and runs to the next ``^Line\\s+M\\s*$`` boundary or
    the end of the document.  Returns the section body verbatim
    (whitespace-stripped on both ends).
    """
    boundaries = [
        (m.start(), m.end(), int(re.search(r"\d+", m.group("num")).group(0)))
        for m in _SC_RULING_ENTRY_RE.finditer(text)
        if re.search(r"\d+", m.group("num"))
    ]
    for i, (b_start, b_end, b_num) in enumerate(boundaries):
        if b_num != line_num:
            continue
        section_start = b_end
        section_end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        section = text[section_start:section_end].strip()
        if len(section) < _SC_MIN_ENTRY_BODY_LEN:
            return None
        return section
    return None


def _split_rulings_format_b(text: str, pdf_bytes: bytes) -> list[SplitRuling]:
    """Format-B splitter — pdfplumber ``extract_tables()`` on summary tables.

    See module docstring §B for the exact pre-conditions.  Returns an
    empty list when no format-B summary table is found (the caller
    should fall back to format A).

    The parser walks every page of the PDF, looking for a table whose
    first row matches the format-B header
    (``LINE | CASE NO. | CASE TITLE | TENTATIVE RULING``).  Within each
    matching table:

    * A "case row" is a row whose CASE NO. column normalizes to a
      single ``\\d{2}(CV|PR)\\d{6}`` token.  Such a row starts a new
      ``SplitRuling``.
    * Continuation rows (where CASE NO. is empty / ``None``) append
      their TENTATIVE RULING column to the current case's
      ``ruling_text``.
    * Cells containing ``See Line N below`` (or similar) cause the
      format-A body section for ``Line N``, when present, to be
      appended after the table cell — preserving the long-form ruling
      content the summary table cross-references.

    ``ruling_index`` is assigned 1..N in the order rows are seen; this
    is independent of the printed ``LINE`` column (which can be
    discontinuous in the live PDFs, e.g. ``;``, ``1,2``, ``3-5``).

    Robustness: any ``Exception`` from pdfplumber (corrupt PDF,
    encrypted PDF, unexpected layout) returns ``[]`` so the caller
    can fall back to format A or the LLM path.  The worker logs the
    fall-through.
    """
    import io

    rulings: list[SplitRuling] = []
    next_index = 1
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables() or []
                for table in tables:
                    if not table or not _is_format_b_header_row(table[0]):
                        continue
                    current: SplitRuling | None = None
                    for row in table[1:]:
                        if not row:
                            continue
                        # Case-no column (col 1).
                        cn_raw = _normalize_table_cell(row[1] if len(row) > 1 else None)
                        cn_normalized = cn_raw.replace(" ", "").upper()
                        cn_match = _SC_FORMAT_B_CASE_NO_RE.match(cn_normalized)
                        if cn_match:
                            # New case row — flush the previous case (if any)
                            # and start fresh.
                            if current is not None:
                                rulings.append(current)
                            title = _normalize_table_cell(row[2] if len(row) > 2 else None)
                            ruling_chunk = _normalize_table_cell(row[3] if len(row) > 3 else None)
                            current = SplitRuling(
                                ruling_index=next_index,
                                case_number=cn_match.group("case_number").upper(),
                                ruling_text=ruling_chunk,
                                case_title=title or None,
                            )
                            next_index += 1
                        elif current is not None:
                            # Continuation row — append the TENTATIVE RULING
                            # column to the current case's ruling_text and
                            # the CASE TITLE column (if non-empty) to the
                            # current case's title (the dept-6 layout wraps
                            # long titles across multiple rows).
                            cont_title = _normalize_table_cell(row[2] if len(row) > 2 else None)
                            cont_ruling = _normalize_table_cell(row[3] if len(row) > 3 else None)
                            if cont_title:
                                current.case_title = (
                                    f"{current.case_title} {cont_title}".strip()
                                    if current.case_title
                                    else cont_title
                                )
                            if cont_ruling:
                                current.ruling_text = (
                                    f"{current.ruling_text}\n{cont_ruling}"
                                    if current.ruling_text
                                    else cont_ruling
                                )
                    # Flush the last case in this table.
                    if current is not None:
                        rulings.append(current)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "format_b_table_parse_error",
            error=str(exc),
        )
        return []

    if not rulings:
        return []

    # Expand ``See Line N below`` cross-references — when a summary-table
    # cell points at a format-A body section, append that section to the
    # row's ruling_text so the per-entry LLM enrichment sees the full
    # long-form ruling, not just the cross-reference.
    for r in rulings:
        if not r.ruling_text:
            continue
        for m in _SC_FORMAT_B_CROSSREF_RE.finditer(r.ruling_text):
            try:
                line_num = int(m.group("num"))
            except ValueError:
                continue
            section = _format_a_body_section(text, line_num)
            if section:
                r.ruling_text = f"{r.ruling_text}\n\n{section}"

    return rulings


def _split_rulings(text: str, pdf_bytes: bytes | None = None) -> list[SplitRuling]:
    """Split Santa Clara multi-ruling PDF text into per-entry ``SplitRuling`` objects.

    Two parsing paths cover the two known SC layouts (see module docstring
    §A and §B):

    * **Format A** — ``Line N`` boundary regex on the flattened pdfplumber
      text.  Always tried first; succeeds for dept 16 and other
      departments that print per-case body sections with bare ``Line N``
      headings.  When this path returns >= 2 entries, those are used
      verbatim.
    * **Format B** — pdfplumber ``extract_tables()`` on the original PDF
      bytes.  Only attempted when format A returns < 2 entries AND the
      flattened text contains the literal
      ``LINE CASE NO. CASE TITLE TENTATIVE RULING`` header AND
      ``pdf_bytes`` is supplied.  Each row of the summary table whose
      CASE NO. column is non-empty becomes one ``SplitRuling``; the
      TENTATIVE RULING column (plus continuation rows and any
      cross-referenced format-A body sections) becomes ``ruling_text``.

    Backward compat: callers that pass only ``text`` (the original
    signature) still work — they get the format-A behavior, which is
    correct for every layout that has per-case ``Line N`` boundaries
    AND for any caller (e.g. the worker pre-#4341) that hasn't been
    updated to thread PDF bytes through.

    The page-1 preamble (calendar header, judge info, courtroom rules,
    appearance instructions) is excluded by both paths — format A
    anchors on the first ``^Line\\s+\\d+\\s*$`` boundary, format B
    anchors on the table header row inside the structured-table output.

    Returns an empty list if neither path can identify multi-ruling
    structure, which is the expected outcome for genuinely single-
    ruling PDFs.  Single-element returns are also possible; the worker
    treats both cases identically (fall through to the LLM path)
    because there is no cross-entry carry-forward window with 0 or 1
    entries.

    Each returned ``SplitRuling`` has:

      * ``ruling_index`` — the integer from the entry header (format A)
        or a 1..N positional counter assigned in row order
        (format B).  Format A indices match the printed ``Line N``;
        format B indices are positional because the printed ``LINE``
        column in the summary table is non-contiguous (``;``, ``1,2``,
        ``3-5``, etc.).
      * ``case_number`` — extracted from the ``Case No.:`` header
        (format A) or the CASE NO. column (format B); always uppercased
        and whitespace-stripped.
      * ``case_title`` — extracted from the ``Case Name:`` header
        (format A) or the CASE TITLE column (format B); always
        whitespace-normalized.
      * ``ruling_text`` — the verbatim entry text (format A) or the
        TENTATIVE RULING column plus continuation rows plus any
        cross-referenced ``Line N`` body sections (format B).

    motion_type and outcome are left ``None`` on purpose — Santa Clara
    PDFs do not carry structured motion-type / outcome labels in their
    per-entry headers, so deterministic regex extraction would produce a
    high false-negative rate.  Letting the framework ``LlmExtractor``
    populate those fields via per-entry enrichment matches the Riverside
    pattern (#3649) and preserves correctness on single-ruling PDFs.
    """
    # Format A always runs first.  When it produces a usable multi-entry
    # result, return it verbatim.
    format_a = _split_rulings_format_a(text)
    if len(format_a) >= 2:
        return format_a

    # Format B is conditional: needs the PDF bytes AND the literal
    # summary-table header in the flattened text.  Otherwise we fall
    # back to whatever format A produced (likely empty or single-entry).
    if pdf_bytes is not None and _SC_FORMAT_B_HEADER_RE.search(text):
        format_b = _split_rulings_format_b(text, pdf_bytes)
        if len(format_b) >= 2:
            return format_b

    return format_a


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


# Extra scraper_ids under which this module's ``_split_rulings`` /
# ``_llm_extract_rulings`` should be registered.  Required so audit / drain
# scripts that key on ``documents.scraper_id`` resolve the splitter on
# rebuild-path rows (rows reconstructed from S3 by ``scripts/rebuild_db.py``,
# which emits ``rebuild-ca-santa_clara`` instead of the live ``ca-sc-...`` id).
# See #4331.
_SPLIT_REGISTRY_ALIASES: list[str] = ["rebuild-ca-santa_clara"]


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
