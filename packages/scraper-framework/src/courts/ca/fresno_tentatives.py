"""Fresno County Superior Court — Civil Tentative Rulings Scraper (Pattern 2).

Verified against live site 2026-03-11:
  URL:  https://www.fresno.courts.ca.gov/online-services/tentative-rulings
  20 PDF links found on index page (across Depts 403, 501, 502, 503)
  No Playwright required; httpx fetch works directly.

NOTE: The old domain fresnosuperiorcourt.org is compromised and redirects to
a gambling site.  The correct domain is fresno.courts.ca.gov.

Page structure:
  <h3>Dept. 403</h3>
  <ul>
    <li><a href="/system/files/tentative-rulings/03-10-26-dept-403.pdf">March 10, 2026</a></li>
    ...
  </ul>
  <h3>Dept. 501</h3>
  ...

Link text format: date only — e.g. "March 10, 2026"
  No judge name or department in link text; department is extracted from the
  PDF filename (e.g. "03-10-26-dept-403.pdf" → department "403").

PDF URL pattern: /system/files/tentative-rulings/{MM}-{DD}-{YY}-dept-{NNN}[_N].pdf
  The optional "_N" suffix (e.g. "_0") appears on revised uploads.

PDF structure (multi-ruling, similar to Riverside):
  Page 1: boilerplate header with "Tentative Rulings for March 10, 2026\nDepartment 403"
  Page 2: "Tentative Rulings for Department 403\nBegin at the next page"
  Page 3+: Individual rulings, each starting with:
    (NN) Tentative Ruling
    Re: Lopez v. Fresno Unified School District
    Superior Court Case No. 25CECG03271
    Hearing Date: March 10, 2026 (Dept. 403)
    Motion: Demurrer to First Amended Complaint
    ...
    Tentative Ruling:
    To sustain ...
    ...
    Issued By: lmg on 3-9-26 .
    (Judge's initials) (Date)

Case number format: 2-digit year + CECG + 4-5 digits
  e.g. 25CECG03271, 23CECG05097, 26CECG00722

Judge names: Not available — PDFs contain only judge initials (e.g. "lmg", "DTT",
  "KCK") in the "Issued By:" line.  No full judge names appear in the link text
  or PDF text.  The judge_name field will be left as None.

Departments: 403, 501, 502, 503 (4 active as of 2026-03)
Posting time: 3:00 PM day before hearing
Platform: JCC/Drupal

Investigation: #155
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx
import structlog

from framework import CapturedDocument, ContentFormat, ScheduleWindow, ScraperConfig

from .pdf_link_scraper import PdfLinkConfig, PdfLinkScraper, _extract_pdf_text

logger = structlog.get_logger(__name__)

INDEX_URL = "https://www.fresno.courts.ca.gov/online-services/tentative-rulings"
BASE_URL = "https://www.fresno.courts.ca.gov"

# Link text is a date string: "March 10, 2026" — no department or judge.
# We capture the department from the filename instead.
# The regex applied to link text extracts nothing useful; instead we use
# _fresno_dept_from_filename below to get the department from the href.
_LINK_TEXT_RE = re.compile(
    r"(?P<department>PLACEHOLDER_NEVER_MATCHES)",
)

# Department from PDF filename: "03-10-26-dept-403.pdf" → "403"
_FILENAME_DEPT_RE = re.compile(
    r"dept-(?P<department>\d+)",
    re.IGNORECASE,
)

# Hearing date from filename: "03-10-26-dept-403.pdf" → MM-DD-YY
_FILENAME_DATE_RE = re.compile(
    r"^(\d{2})-(\d{2})-(\d{2})-dept-",
)

# Hearing date from PDF text: "Tentative Rulings for March 10, 2026"
_HEARING_DATE_RE = re.compile(
    r"(?:Tentative Rulings?\s+(?:for\s+)?)"
    r"(?P<date>(?:January|February|March|April|May|June|July|August"
    r"|September|October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

# Case number: 2-digit year + CECG + 4-5 digits (e.g. 25CECG03271)
_CASE_NUMBER_RE = re.compile(r"\b\d{2}CECG\d{4,5}\b")

# Ruling entry boundary: "(NN) Tentative Ruling" or "(NN)\nTentative Ruling"
# Must be followed by "Tentative Ruling" (same line or next line) to avoid
# matching parenthetical numbers in legal body text.
_RULING_ENTRY_RE = re.compile(
    r"^\((?P<num>\d{1,3})\)\s*Tentative\s+Ruling|^\((?P<num2>\d{1,3})\)\s*\n\s*Tentative\s+Ruling",
    re.MULTILINE,
)

# Case title from "Re: ..." line
_CASE_TITLE_RE = re.compile(
    r"^Re:\s+(?P<title>.+?)$",
    re.MULTILINE,
)

# Case number from "Superior Court Case No. ..." or "Case No. ..." or "Court Case No. ..."
_STRUCTURED_CASE_NO_RE = re.compile(
    r"(?:Superior\s+Court\s+)?(?:Court\s+)?Case\s+No\.\s*(?P<case_no>\S+)",
    re.IGNORECASE,
)

# Hearing date from ruling header: "Hearing Date: March 10, 2026 (Dept. 403)"
_RULING_HEARING_DATE_RE = re.compile(
    r"Hearing\s+Date:\s+(?P<date>(?:January|February|March|April|May|June|July|August"
    r"|September|October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

# Motion type from "Motion: ..." line (may span multiple lines until next field)
_MOTION_RE = re.compile(
    r"^Motion(?:s?\s*\(x\d+\))?:\s*(?P<motion>.+?)$",
    re.MULTILINE,
)

# Outcome from "Tentative Ruling:\n" followed by disposition text.
# We look for the SECOND occurrence (the first is just the section header).
_OUTCOME_RE = re.compile(
    r"Tentative Ruling:\s*\n?\s*(?P<outcome>.+?)(?:\n|$)",
    re.IGNORECASE,
)

# "No Tentative Rulings" boilerplate detection
_NO_RULINGS_RE = re.compile(
    r"^\s*No\s+Tentative\s+Rulings?\b",
    re.IGNORECASE,
)

# "Issued By:" line — only initials, no full judge name
_ISSUED_BY_RE = re.compile(
    r"Issued\s+By:\s+(?P<initials>\S+)\s+on\s+",
    re.IGNORECASE,
)


class SplitRuling:
    """A single ruling extracted from a multi-ruling Fresno PDF."""

    __slots__ = (
        "ruling_index",
        "case_number",
        "ruling_text",
        "case_title",
        "motion_type",
        "outcome",
        "hearing_date",
    )

    def __init__(
        self,
        ruling_index: int,
        case_number: str | None,
        ruling_text: str,
        case_title: str | None,
        motion_type: str | None,
        outcome: str | None,
        hearing_date: datetime | None,
    ) -> None:
        self.ruling_index = ruling_index
        self.case_number = case_number
        self.ruling_text = ruling_text
        self.case_title = case_title
        self.motion_type = motion_type
        self.outcome = outcome
        self.hearing_date = hearing_date


def _fresno_dept_from_filename(filename: str) -> str | None:
    """Extract department code from filename like '03-10-26-dept-403.pdf' → '403'."""
    m = _FILENAME_DEPT_RE.search(filename)
    return m.group("department") if m else None


def _fresno_hearing_date_from_filename(filename: str) -> datetime | None:
    """Extract hearing date from filename like '03-10-26-dept-403.pdf' → 2026-03-10."""
    m = _FILENAME_DATE_RE.search(filename)
    if not m:
        return None
    try:
        month = int(m.group(1))
        day = int(m.group(2))
        year = 2000 + int(m.group(3))
        return datetime(year, month, day)
    except (ValueError, IndexError):
        return None


def _fresno_hearing_date_from_text(text: str) -> datetime | None:
    """Extract hearing date from Fresno PDF text or ruling header."""
    # Try ruling-specific header first
    m = _RULING_HEARING_DATE_RE.search(text)
    if not m:
        # Fall back to PDF-wide header
        m = _HEARING_DATE_RE.search(text)
    if not m:
        return None
    raw = " ".join(m.group("date").split())
    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _extract_case_title(text: str) -> str | None:
    """Extract case title from 'Re: ...' line in ruling header."""
    m = _CASE_TITLE_RE.search(text[:500])
    if not m:
        return None
    title = m.group("title").strip()
    # Clean up any trailing whitespace or artifacts
    title = " ".join(title.split())
    return title if title else None


def _extract_case_number(text: str) -> str | None:
    """Extract case number from structured header or regex fallback."""
    # Try structured "Case No." line first
    m = _STRUCTURED_CASE_NO_RE.search(text[:500])
    if m:
        return m.group("case_no").strip()
    # Fallback: regex for CECG case numbers
    m2 = _CASE_NUMBER_RE.search(text)
    return m2.group(0) if m2 else None


def _extract_motion_type(text: str) -> str | None:
    """Extract motion type from 'Motion: ...' line in ruling header.

    The motion description may span multiple lines in the header area
    (before the 'Tentative Ruling:' line).
    """
    # Find the motion line
    m = _MOTION_RE.search(text[:1000])
    if not m:
        return None

    # Get the first line of the motion
    motion = m.group("motion").strip()

    # Check if the motion continues on the next line(s) before
    # "Tentative Ruling:" or "If oral argument"
    motion_end = m.end()
    remaining = text[motion_end : motion_end + 500]

    # Collect continuation lines (indented or not starting with a keyword)
    for line in remaining.split("\n"):
        stripped = line.strip()
        if not stripped:
            break
        # Stop at known field markers
        if any(
            stripped.startswith(kw)
            for kw in (
                "Tentative Ruling:",
                "If oral argument",
                "Hearing Date:",
                "Superior Court",
                "Case No.",
                "Court Case",
            )
        ):
            break
        motion += " " + stripped

    motion = " ".join(motion.split()).strip()
    # Truncate at 120 chars for sanity
    if len(motion) > 120:
        motion = motion[:120].rsplit(" ", 1)[0]
    return motion if motion else None


def _extract_outcome(text: str) -> str | None:
    """Extract the ruling outcome from 'Tentative Ruling:\\n...' blocks.

    Fresno PDFs have a structured format:
      Tentative Ruling:
      To grant. / To deny. / To sustain ... / etc.

    We look for the LAST "Tentative Ruling:" block in the ruling text
    (the first may be just the section header), but for Fresno the
    disposition always follows "Tentative Ruling:\\n" with "To ..." text.
    """
    # Find all "Tentative Ruling:" occurrences
    matches = list(_OUTCOME_RE.finditer(text))
    if not matches:
        return None

    # Use the first match that has a substantive outcome (starts with "To ")
    for m in matches:
        raw = m.group("outcome").strip()
        if raw.lower().startswith("to "):
            # Normalize: extract the core disposition
            return _normalize_outcome(raw)

    # Fallback: scan for disposition keywords
    text_lower = text.lower()
    disposition_re = re.compile(
        r"\b(?:is\s+)?(?P<outcome>overruled|sustained|granted|denied|moot)\b",
        re.IGNORECASE,
    )
    disposition_matches = list(disposition_re.finditer(text))
    if disposition_matches:
        return disposition_matches[-1].group("outcome").capitalize()

    if re.search(r"\bcontinued\s+to\b", text_lower):
        return "Continued"

    if "off calendar" in text_lower or "taken off calendar" in text_lower:
        return "Off Calendar"

    return None


def _normalize_outcome(raw: str) -> str:
    """Normalize a 'To ...' outcome string to a canonical form."""
    lower = raw.lower()

    if lower.startswith("to grant"):
        if "in part" in lower:
            return "Granted in Part"
        return "Granted"
    if lower.startswith("to deny"):
        if "without prejudice" in lower:
            return "Denied without Prejudice"
        return "Denied"
    if lower.startswith("to sustain"):
        if "without leave" in lower:
            return "Sustained without Leave to Amend"
        if "with leave" in lower:
            return "Sustained with Leave to Amend"
        return "Sustained"
    if lower.startswith("to overrule"):
        return "Overruled"
    if "off calendar" in lower:
        return "Off Calendar"
    if lower.startswith("to continue"):
        return "Continued"
    if lower.startswith("to take") and "off calendar" in lower:
        return "Off Calendar"
    if lower.startswith("to order") and "off calendar" in lower:
        return "Off Calendar"
    if lower.startswith("to stay"):
        return "Stayed"

    # Truncate at first period for short outcomes
    period_idx = raw.find(".")
    if 0 < period_idx < 60:
        return raw[:period_idx].strip()
    if len(raw) > 60:
        return raw[:60].rsplit(" ", 1)[0]
    return raw.strip()


def _split_rulings(text: str) -> list[SplitRuling]:
    """Split PDF text containing multiple numbered rulings into SplitRuling objects.

    Fresno PDFs use numbered entries like:
        (20) Tentative Ruling
        Re: Lopez v. Fresno Unified School District
        Superior Court Case No. 25CECG03271
        Hearing Date: March 10, 2026 (Dept. 403)
        Motion: Demurrer to First Amended Complaint
        ...
        Tentative Ruling:
        To sustain ...

    Returns an empty list if no numbered entries are found.
    """
    matches = list(_RULING_ENTRY_RE.finditer(text))
    if not matches:
        return []

    rulings: list[SplitRuling] = []
    for i, match in enumerate(matches):
        entry_num = int(match.group("num") or match.group("num2"))
        start = match.end()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(text)

        ruling_text = text[start:end].strip()

        # Skip if this is just a boilerplate "Tentative Ruling" header
        if len(ruling_text) < 50:
            continue

        # Check for "Tentative Ruling" immediately following the number
        # (already consumed by the regex group)
        if ruling_text.startswith("Tentative Ruling"):
            ruling_text = ruling_text[len("Tentative Ruling") :].strip()

        # Remove page number footers
        ruling_text = re.sub(r"\n\d{1,3}\s*$", "", ruling_text).strip()

        case_number = _extract_case_number(ruling_text)
        case_title = _extract_case_title(ruling_text)
        motion_type = _extract_motion_type(ruling_text)
        outcome = _extract_outcome(ruling_text)
        hearing_date = _fresno_hearing_date_from_text(ruling_text)

        rulings.append(
            SplitRuling(
                ruling_index=entry_num,
                case_number=case_number,
                ruling_text=ruling_text,
                case_title=case_title,
                motion_type=motion_type,
                outcome=outcome,
                hearing_date=hearing_date,
            )
        )

    return rulings


def _fresno_courthouse(_dept: str) -> str | None:
    """All Fresno civil departments are in the same courthouse."""
    return "B.F. Sisk Federal Courthouse"


class FresnoTentativeRulingsScraper(PdfLinkScraper):
    """Fresno County civil tentative rulings — PDF-link pattern.

    Department is extracted from the PDF filename.  Judge names are not
    available (PDFs contain only initials).  Each PDF may contain multiple
    rulings, split into individual CapturedDocument records.

    Overrides ``_fetch_one_pdf`` to extract department from the URL filename
    (since the link text contains only the date), and ``fetch_documents`` to
    split multi-ruling PDFs.
    """

    def __init__(self, config: ScraperConfig, **kwargs: Any) -> None:
        pdf_config = PdfLinkConfig(
            index_url=INDEX_URL,
            pdf_base_url=BASE_URL,
            link_text_re=_LINK_TEXT_RE,
            courthouse_from_dept=_fresno_courthouse,
            verify_ssl=True,
            filter_by_link_text=False,  # link text is a date, not dept/judge
            case_number_re=_CASE_NUMBER_RE,
        )
        super().__init__(config, pdf_config=pdf_config, **kwargs)

    def _fetch_one_pdf(
        self,
        client: httpx.Client,
        href: str,
        link_text: str,
    ) -> CapturedDocument:
        """Override to extract department from filename rather than link text."""
        # Extract department from the URL filename
        filename = href.rsplit("/", 1)[-1] if "/" in href else href
        department = _fresno_dept_from_filename(filename)

        courthouse: str | None = None
        if department and self._pdf_config.courthouse_from_dept:
            courthouse = self._pdf_config.courthouse_from_dept(department)

        response = client.get(href)
        response.raise_for_status()

        doc = self._make_base_doc(
            source_url=href,
            raw_content=response.content,
            content_format=ContentFormat.PDF,
        )
        doc.department = department
        doc.courthouse = courthouse
        doc.extra["link_text"] = link_text
        doc.extra["filename"] = filename
        return doc

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch PDFs and split multi-ruling PDFs into individual documents."""
        raw_docs = super().fetch_documents()
        split_docs: list[CapturedDocument] = []

        for doc in raw_docs:
            try:
                text = _extract_pdf_text(doc.raw_content)
            except Exception as exc:
                logger.warning("PDF text extraction failed", error=str(exc))
                split_docs.append(doc)
                continue

            rulings = _split_rulings(text)
            if len(rulings) <= 1:
                # Single ruling or no rulings — keep original doc
                # Still try to extract fields from a single ruling
                if len(rulings) == 1:
                    r = rulings[0]
                    doc.case_number = r.case_number
                    doc.case_title = r.case_title
                    doc.motion_type = r.motion_type
                    doc.outcome = r.outcome
                    doc.ruling_text = r.ruling_text
                    doc.hearing_date = r.hearing_date
                split_docs.append(doc)
                continue

            # Extract hearing date from the full PDF header
            pdf_hearing_date = _fresno_hearing_date_from_text(text)

            logger.info(
                "Splitting multi-ruling PDF",
                department=doc.department,
                ruling_count=len(rulings),
            )
            for ruling in rulings:
                child = self._make_base_doc(
                    source_url=doc.source_url,
                    raw_content=doc.raw_content,
                    content_format=ContentFormat.PDF,
                )
                # Preserve parent metadata
                child.department = doc.department
                child.courthouse = doc.courthouse
                child.extra = {**doc.extra}
                # Set per-ruling fields
                child.case_number = ruling.case_number
                child.ruling_text = ruling.ruling_text
                child.case_title = ruling.case_title
                child.motion_type = ruling.motion_type
                child.outcome = ruling.outcome
                child.hearing_date = ruling.hearing_date or pdf_hearing_date
                child.extra["ruling_index"] = ruling.ruling_index
                child.extra["pre_split"] = True
                split_docs.append(child)

        return split_docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract fields from PDF text.

        If the document was pre-split by fetch_documents, skip the parent's
        parse_document and only fill in missing fields.
        """
        if doc.extra.get("pre_split"):
            # Already split — fields already populated
            # Extract hearing date from filename as fallback
            if not doc.hearing_date:
                filename = doc.extra.get("filename", "")
                doc.hearing_date = _fresno_hearing_date_from_filename(filename)
            return doc

        # Single-ruling PDF: use parent parse logic then add Fresno-specific fields
        doc = super().parse_document(doc)

        if doc.ruling_text and not doc.hearing_date:
            doc.hearing_date = _fresno_hearing_date_from_text(doc.ruling_text)

        if not doc.hearing_date:
            filename = doc.extra.get("filename", "")
            doc.hearing_date = _fresno_hearing_date_from_filename(filename)

        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default Fresno tentative rulings scraper configuration."""
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-fresno-tentatives-civil",
        state="CA",
        county="Fresno",
        court="Superior Court",
        target_urls=[INDEX_URL],
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
