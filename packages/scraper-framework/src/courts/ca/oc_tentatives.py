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
from datetime import datetime
from typing import Any

import httpx

from framework import CapturedDocument, ScheduleWindow, ScraperConfig

from .pdf_link_scraper import PdfLinkConfig, PdfLinkScraper

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


class OCTentativeRulingsScraper(PdfLinkScraper):
    """Orange County civil tentative rulings — PDF-link pattern."""

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
        """Extract case numbers (via super) and hearing date from PDF text."""
        doc = super().parse_document(doc)

        # Extract hearing date from PDF text
        if doc.ruling_text and not doc.hearing_date:
            doc.hearing_date = _oc_hearing_date_from_text(doc.ruling_text)

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
