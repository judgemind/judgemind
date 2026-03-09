"""Orange County Superior Court — Civil Tentative Rulings Scraper (Pattern 2).

Verified against live site 2026-03-02:
  URL:  https://www.occourts.org/online-services/tentative-rulings/civil-tentative-rulings
  33 PDF links found on index page

Link text format: "LASTNAME, Firstname [I.] - Dept CODE"
  e.g. "APKARIAN, Gassia - Dept C25"
  e.g. "HOFFER, David A. - Dept  CX103"  (may contain non-breaking spaces)

PDF URL pattern: https://www.occourts.org/sites/default/files/oc/default/tentative-rulings/{name}rulings.pdf
  Some PDFs link to Pantheon CDN (live-jcc-oc.pantheonsite.io) — these are valid.

PDF structure (Apkarian / Dept C25, 36 pages):
  Page 1 header: "TENTATIVE RULINGS / LAW & MOTION / DEPT C25 / Judge Gassia Apkarian"
  Hearing dates like "February 24, 2026"
  Case rows:  "<line#> <Case Name>  <motion>\n<case_number>  ..."
  Case number formats:
    Central/West:        DD-DDDDDDDD   (e.g. "25-01455183")
    Costa Mesa/Complex:  DDDD-DDDDDDDD (e.g. "2024-01437598")
    North:               No case numbers in PDF text (only line numbers + case names)

Courthouse mapping (derived from dept code prefix):
  CX*  → Complex Civil Department (Laguna Hills)
  C*   → Central Justice Center (Santa Ana)
  N*   → North Justice Center (Fullerton)
  W*   → West Justice Center (Westminster)
  CM*  → Costa Mesa Justice Center
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import structlog

from framework import CapturedDocument, ScheduleWindow, ScraperConfig

from .pdf_link_scraper import PdfLinkConfig, PdfLinkScraper

logger = structlog.get_logger(__name__)

INDEX_URL = "https://www.occourts.org/online-services/tentative-rulings/civil-tentative-rulings"
BASE_URL = "https://www.occourts.org"

# Link text: "LASTNAME, Firstname [I.] - Dept CX101"
# Captures last→first separately so we can reconstruct "Firstname Lastname"
_LINK_TEXT_RE = re.compile(
    r"^(?P<last>[A-Z][A-Z\s']+),\s*(?P<first>[^-]+?)\s*-\s*Dept\.?\s*(?P<department>\S+)",
    re.IGNORECASE,
)


def _oc_link_text_re() -> re.Pattern:
    """OC link text regex with a combined judge_name group for PdfLinkConfig."""
    # PdfLinkConfig expects 'judge_name' and 'department' groups.
    # We post-process in _oc_courthouse to derive judge_name from last+first.
    # Use a wrapper regex that captures all three groups.
    return re.compile(
        r"^(?P<judge_name>(?P<last>[A-Z][A-Z\s']+),\s*(?P<first>[^-]+?))\s*-\s*Dept\.?\s*(?P<department>\S+)",
        re.IGNORECASE,
    )


def _oc_judge_name_from_match(m: re.Match) -> str:
    """Convert 'LASTNAME, Firstname' → 'Firstname Lastname'."""
    last = m.group("last").strip().title()
    first = m.group("first").strip()
    return f"{first} {last}"


# Hearing date from PDF text: "February 24, 2026", "March 06, 2026", etc.
# Match the first occurrence of "Month DD, YYYY" in the extracted PDF text.
_HEARING_DATE_RE = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{1,2},?\s+\d{4}",
    re.IGNORECASE,
)


def _oc_hearing_date_from_text(text: str) -> datetime | None:
    """Extract the first date (Month DD, YYYY) from OC PDF text as hearing date."""
    m = _HEARING_DATE_RE.search(text)
    if not m:
        return None
    raw = m.group(0)
    # Normalize whitespace (some PDFs split across lines)
    raw = " ".join(raw.split())
    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# North Justice Center — case entry parsing
# ---------------------------------------------------------------------------
#
# North JC PDFs do not contain structured case numbers. Instead they have a
# table layout:
#   # Case Name   Tentative
#   101 Alday vs Orange   Motion for Continuance of Trial
#       Coast Title       ...ruling text...
#       Company of
#       Southern
#       California
#
# We extract case titles and motion types from this layout so records are
# meaningful even without a case number.

# Motion-type keywords used to split the case-name column from the tentative
# column on the first line of each entry.
_MOTION_KEYWORDS = (
    "Motion for",
    "Motion to",
    "Demurrer",
    "OSC",
    "Application",
    "Petition",
    "Status Conference",
    "Case Management Conference",
    "CMC REMAINS",
    "OFF CALENDAR",
    "HEARING",
)

# Matches the first line of a case entry: 3-digit line number + rest
_ENTRY_START_RE = re.compile(r"^(\d{3})\s+(.+)", re.MULTILINE)


@dataclass
class _NorthCaseEntry:
    """A single case entry parsed from a North Justice Center PDF."""

    line_num: str
    case_title: str
    motion_type: str | None


def _parse_north_case_entries(text: str) -> list[_NorthCaseEntry]:
    """Parse case entries from North Justice Center PDF text.

    Returns a list of entries with case titles and motion types extracted
    from the columnar layout. Only entries containing "vs" (indicating a
    case name with opposing parties) are included.
    """
    lines = text.split("\n")

    # Find all entry start positions
    entry_positions: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        m = _ENTRY_START_RE.match(line)
        if m:
            entry_positions.append((i, m.group(1), m.group(2)))

    entries: list[_NorthCaseEntry] = []
    for idx, (line_idx, line_num, first_line_rest) in enumerate(entry_positions):
        # Determine where the next entry starts (to bound continuation lines)
        if idx + 1 < len(entry_positions):
            next_entry_line = entry_positions[idx + 1][0]
        else:
            next_entry_line = len(lines)

        # Split first line: case name part | motion type part
        motion_start = len(first_line_rest)
        motion_type: str | None = None
        for kw in _MOTION_KEYWORDS:
            pos = first_line_rest.find(kw)
            if pos != -1 and pos < motion_start:
                motion_start = pos
                motion_type = first_line_rest[pos:].strip()

        case_name_part = first_line_rest[:motion_start].strip()

        # Collect continuation lines that are part of the case name column.
        # These are short lines (< 35 chars) that look like name fragments,
        # not ruling text.  We stop at the next entry, a "Page N of M" marker,
        # or a line that looks like the start of ruling analysis.
        name_parts = [case_name_part]
        for j in range(line_idx + 1, min(next_entry_line, line_idx + 6)):
            cand = lines[j].strip()
            if not cand or cand.startswith("Page "):
                break
            if len(cand) >= 35:
                break
            # Sentence-like lines with periods/parens are ruling text
            if "." in cand or "(" in cand or ")" in cand:
                break
            name_parts.append(cand)

        full_name = " ".join(name_parts)
        full_name = re.sub(r"\s+", " ", full_name).strip()

        # Only keep entries that contain "vs" — these are actual case names
        if re.search(r"\bvs\.?\b", full_name, re.IGNORECASE):
            entries.append(
                _NorthCaseEntry(
                    line_num=line_num,
                    case_title=full_name,
                    motion_type=motion_type,
                )
            )

    return entries


def _is_north_dept(dept: str | None) -> bool:
    """Return True if the department code indicates North Justice Center."""
    if not dept:
        return False
    return dept.upper().strip().startswith("N")


def _oc_courthouse(dept: str) -> str:
    dept = dept.upper().strip()
    if dept.startswith("CX"):
        return "Complex Civil Department"
    if dept.startswith("CM"):
        return "Costa Mesa Justice Center"
    if dept.startswith("N"):
        return "North Justice Center"
    if dept.startswith("W"):
        return "West Justice Center"
    # C10–C65 etc.
    return "Central Justice Center"


_EMPTY_CASE_TABLE_RE = re.compile(
    r"TENTATIVE\s+RULINGS?\s*\n"
    r"(?:LAW\s*[&]\s*MOTION\s*\n)?"
    r"(?:DEPT\s+\S+\s*\n)?"
    r"(?:[^\n]+\n){0,5}"  # up to 5 header lines
    r"Date:\s*\n",
    re.IGNORECASE,
)


class OCTentativeRulingsScraper(PdfLinkScraper):
    """Orange County civil tentative rulings — PDF-link pattern."""

    def _is_boilerplate(self, text: str) -> bool:
        """Detect OC civil boilerplate PDFs (#322).

        OC may publish placeholder PDFs with department headers and
        procedural instructions but no actual rulings.  These have the
        TENTATIVE RULINGS header with an empty Date: field and no case
        numbers (neither the DD-DDDDDDDD nor DDDD-DDDDDDDD formats).
        """
        if super()._is_boilerplate(text):
            return True

        if _EMPTY_CASE_TABLE_RE.search(text) and not self._pdf_config.case_number_re.search(text):
            return True

        return False

    def __init__(self, config: ScraperConfig, **kwargs: Any) -> None:
        pdf_config = PdfLinkConfig(
            index_url=INDEX_URL,
            pdf_base_url=BASE_URL,
            link_text_re=_oc_link_text_re(),
            courthouse_from_dept=_oc_courthouse,
            verify_ssl=True,
            # Central/West use DD-DDDDDDDD; Costa Mesa/Complex use DDDD-DDDDDDDD
            case_number_re=re.compile(r"\b\d{2,4}-\d{8}\b"),
        )
        super().__init__(config, pdf_config=pdf_config, **kwargs)

    def _fetch_one_pdf(self, client: httpx.Client, href: str, link_text: str) -> CapturedDocument:
        """Override to reconstruct judge name as 'Firstname Lastname'."""
        doc = super()._fetch_one_pdf(client, href, link_text)

        # Re-parse link text to get proper name order
        m = _LINK_TEXT_RE.match(link_text)
        if m:
            last = m.group("last").strip().title()
            first = m.group("first").strip()
            doc.judge_name = f"{first} {last}"

        return doc

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract case numbers, hearing date, and case titles from PDF text.

        For North Justice Center PDFs (departments starting with "N"), case
        numbers are not present in the PDF text. Instead we parse the columnar
        layout to extract case titles and motion types, making these records
        useful for legal research even without formal case numbers.
        """
        doc = super().parse_document(doc)

        # Extract hearing date from PDF text
        if doc.ruling_text and not doc.hearing_date:
            doc.hearing_date = _oc_hearing_date_from_text(doc.ruling_text)

        # North Justice Center: extract case titles from the columnar layout
        if doc.ruling_text and _is_north_dept(doc.department):
            entries = _parse_north_case_entries(doc.ruling_text)
            if entries:
                # Set primary case_title to the first entry
                if not doc.case_title:
                    doc.case_title = entries[0].case_title
                # Set primary motion_type to the first entry
                if not doc.motion_type and entries[0].motion_type:
                    doc.motion_type = entries[0].motion_type
                # Store all titles and motion types in extra for downstream use
                doc.extra["case_titles"] = [e.case_title for e in entries]
                doc.extra["motion_types"] = [e.motion_type for e in entries if e.motion_type]
                logger.info(
                    "Extracted case titles from North JC PDF",
                    department=doc.department,
                    count=len(entries),
                    first_title=entries[0].case_title,
                )

        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-oc-tentatives-civil",
        state="CA",
        county="Orange",
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
