"""Orange County Superior Court — Family Law Tentative Rulings Scraper (Pattern 2).

Verified against live site 2026-03-07:
  URL:  https://www.occourts.org/online-services/tentative-rulings/family-law-tentative-rulings
  2 PDF links found on index page

Link text format: "LASTNAME, Firstname [I.] - Dept CODE"
  e.g. "CLAUSTRO, Israel - Dept C22"
  e.g. "KOHLER, Robert - Dept. L69"

PDF URL pattern: https://www.occourts.org/sites/default/files/oc/default/tentative-rulings/{name}rulings.pdf
  Some PDFs link to Pantheon CDN (live-jcc-oc.pantheonsite.io) — these are valid.

PDF structure (Claustro / Dept C22, 1 page):
  Page 1 header: "CENTRAL JUSTICE CENTER / DEPARTMENT C22 / Judge Israel Claustro"
  "TENTATIVE RULINGS / Date: December 5, 2025"
  Case rows:  "# Case Name TENTATIVE RULING"
    then:  "<line#> <PARTY V. PARTY>  <ruling text>"
    then:  "<case_number>"
  Case number format: DD + "D" + DDDDDD (e.g. "25D006297")

Courthouse mapping (derived from dept code prefix):
  C*  → Central Justice Center (Santa Ana)
  L*  → Lamoreaux Justice Center (Orange)
  N*  → North Justice Center (Fullerton)
  W*  → West Justice Center (Westminster)

Investigation: #142
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from framework import CapturedDocument, ScheduleWindow, ScraperConfig

from .pdf_link_scraper import PdfLinkConfig, PdfLinkScraper

INDEX_URL = (
    "https://www.occourts.org/online-services/tentative-rulings/family-law-tentative-rulings"
)
BASE_URL = "https://www.occourts.org"

# Link text: "LASTNAME, Firstname [I.] - Dept C22"
# Same pattern as civil — reuse the regex structure.
_LINK_TEXT_RE = re.compile(
    r"^(?P<last>[A-Z][A-Z\s']+),\s*(?P<first>[^-]+?)\s*-\s*Dept\.?\s*(?P<department>\S+)",
    re.IGNORECASE,
)


def _oc_fl_link_text_re() -> re.Pattern:
    """OC Family Law link text regex with combined judge_name group for PdfLinkConfig."""
    return re.compile(
        r"^(?P<judge_name>(?P<last>[A-Z][A-Z\s']+),\s*(?P<first>[^-]+?))\s*-\s*Dept\.?\s*(?P<department>\S+)",
        re.IGNORECASE,
    )


def _oc_fl_judge_name_from_match(m: re.Match) -> str:
    """Convert 'LASTNAME, Firstname' → 'Firstname Lastname'."""
    last = m.group("last").strip().title()
    first = m.group("first").strip()
    return f"{first} {last}"


# Hearing date from PDF text: "Date: December 5, 2025"
_HEARING_DATE_RE = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{1,2},?\s+\d{4}",
)

# Case number: 2-digit year + "D" + 6 digits (e.g. "25D006297")
_CASE_NUMBER_RE = re.compile(r"\b\d{2}D\d{6}\b")

# Case title: "PARTY V. PARTY" or "PARTY v. PARTY" pattern
# Captures up to the next lowercase word or end of uppercase sequence after "V."
_CASE_TITLE_RE = re.compile(
    r"^\d+\s+(?P<title>[A-Z][A-Z'\s-]+(?:V\.|v\.)\s+[A-Z][A-Z'\s-]+?)(?:\s+(?:[A-Z][a-z]|No\s|TENTATIVE)|\s*$)",
    re.MULTILINE,
)

# Outcome keywords in ruling text
_OUTCOME_RE = re.compile(
    r"\b(GRANTED|DENIED|CONTINUED|MOOT|OFF\s+CALENDAR)\b",
    re.IGNORECASE,
)


def _oc_fl_hearing_date_from_text(text: str) -> datetime | None:
    """Extract the first date (Month DD, YYYY) from OC Family Law PDF text."""
    m = _HEARING_DATE_RE.search(text)
    if not m:
        return None
    raw = " ".join(m.group(0).split())
    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _oc_fl_case_title_from_text(text: str) -> str | None:
    """Extract the first case title (PARTY V. PARTY) from PDF text."""
    m = _CASE_TITLE_RE.search(text)
    if m:
        raw = m.group("title").strip()
        # Normalize whitespace
        return " ".join(raw.split())
    return None


def _oc_fl_outcome_from_text(text: str) -> str | None:
    """Extract the first outcome keyword from PDF text."""
    m = _OUTCOME_RE.search(text)
    if m:
        return m.group(1).strip().upper()
    return None


def _oc_fl_motion_type_from_text(text: str) -> str | None:
    """Extract motion type from PDF ruling text.

    Family law rulings use "RFO" (Request for Order) as the primary motion type,
    or describe the ruling type directly (e.g. "continue this matter", "off calendar").
    """
    # Look for RFO (Request for Order) — common in family law
    if re.search(r"\bRFO\b", text):
        return "Request for Order"
    # Look for motion references
    motion_match = re.search(
        r"\b(?:MOTION|Motion)\s+(?:for|to|re)\s+([^\n.;]+)",
        text,
    )
    if motion_match:
        return motion_match.group(0).strip()[:100]  # Cap at 100 chars
    return None


def _oc_fl_courthouse(dept: str) -> str:
    """Map a department code to its courthouse name for OC Family Law."""
    dept = dept.upper().strip()
    if dept.startswith("L"):
        return "Lamoreaux Justice Center"
    if dept.startswith("N"):
        return "North Justice Center"
    if dept.startswith("W"):
        return "West Justice Center"
    # C* → Central Justice Center
    return "Central Justice Center"


class OCFamilyLawTentativeRulingsScraper(PdfLinkScraper):
    """Orange County Family Law tentative rulings — PDF-link pattern."""

    def __init__(self, config: ScraperConfig, **kwargs: Any) -> None:
        pdf_config = PdfLinkConfig(
            index_url=INDEX_URL,
            pdf_base_url=BASE_URL,
            link_text_re=_oc_fl_link_text_re(),
            courthouse_from_dept=_oc_fl_courthouse,
            verify_ssl=True,
            case_number_re=_CASE_NUMBER_RE,
        )
        super().__init__(config, pdf_config=pdf_config, **kwargs)

    def _fetch_one_pdf(self, client: httpx.Client, href: str, link_text: str) -> CapturedDocument:
        """Override to reconstruct judge name as 'Firstname Lastname'."""
        doc = super()._fetch_one_pdf(client, href, link_text)

        # Re-parse link text to get proper name order
        m = _LINK_TEXT_RE.match(link_text)
        if m:
            doc.judge_name = _oc_fl_judge_name_from_match(m)

        return doc

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract case numbers, hearing date, case title, outcome, motion type."""
        doc = super().parse_document(doc)

        if doc.ruling_text:
            # Hearing date
            if not doc.hearing_date:
                doc.hearing_date = _oc_fl_hearing_date_from_text(doc.ruling_text)

            # Case title
            if not doc.case_title:
                doc.case_title = _oc_fl_case_title_from_text(doc.ruling_text)

            # Outcome
            if not doc.outcome:
                doc.outcome = _oc_fl_outcome_from_text(doc.ruling_text)

            # Motion type
            if not doc.motion_type:
                doc.motion_type = _oc_fl_motion_type_from_text(doc.ruling_text)

        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default OC Family Law scraper configuration."""
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-oc-tentatives-family-law",
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
