"""San Bernardino Superior Court — Civil Tentative Rulings Scraper (Pattern 2).

Verified against live site 2026-03-02:
  URL:  https://old.sb-court.org/GeneralInfo/TentativeRulings.aspx
  52 PDF links found on index page (spanning ~30 days)
  No Playwright required; httpx fetch works directly.

Link text format: filename only — e.g. "CVS24030426.pdf"
  No judge name or dept in link text; both extracted later from PDF text.

PDF filename pattern: CV{LOC}{DEPT}{MMDDYY}.pdf
  LOC  = location code (first letter): S=San Bernardino, R=Rancho Cucamonga
  DEPT = department number (e.g. 12, 24, 36)
  MMDDYY = month+day+2-digit year

Judge/dept extraction from PDF page 1 text (two observed formats):
  Primary:  "Department R12 - Judge Kory Mathewson"
            (handles regular dash, em-dash, and no-space before dash like "R17- Judge")
  Fallback: "BEFORE THE HONORABLE JOSEPH WIDMAN"
            (title-cased on extraction; used by Dept S36)

Case number format: CIV + 2 uppercase letters + 5-8 digits
  e.g. CIVRS2502080, CIVSB2416631

Courthouse mapping (conservative — only S and R confirmed from fixtures):
  S* → San Bernardino Justice Center
  R* → Rancho Cucamonga Justice Center
"""

from __future__ import annotations

import re
from typing import Any

from framework import CapturedDocument, ScheduleWindow, ScraperConfig

from .pdf_link_scraper import PdfLinkConfig, PdfLinkScraper

INDEX_URL = "https://old.sb-court.org/GeneralInfo/TentativeRulings.aspx"
BASE_URL = "https://old.sb-court.org"

# Link text = filename: "CVS24030426.pdf" → department "S24"
_LINK_TEXT_RE = re.compile(r"CV(?P<department>[A-Z]\d+)\d{6}\.pdf", re.IGNORECASE)

# PDF header primary format: "Department R12 - Judge Kory Mathewson"
# \S+? (non-greedy) handles "R17-" (dash attached) as well as "R12 -" (spaced)
# Character class [-\u2013] covers ASCII hyphen and Unicode en-dash
_DEPT_JUDGE_RE = re.compile(
    r"Department\s+\S+?\s*[-\u2013]\s*Judge\s+(?P<judge_name>[^\n]+)",
    re.IGNORECASE,
)

# PDF header fallback: "BEFORE THE HONORABLE JOSEPH WIDMAN" (Dept S36 format)
_HONORABLE_RE = re.compile(
    r"BEFORE THE HONORABLE\s+(?P<judge_name>[^\n]+)",
    re.IGNORECASE,
)

# Case numbers like "CIVRS2502080", "CIVSB2416631"
_CASE_NUMBER_RE = re.compile(r"\bCIV[A-Z]{2}\d{5,8}\b")


def _sb_judge_from_pdf_text(text: str) -> str | None:
    """Extract judge name from SB PDF page 1 text, trying two known formats."""
    m = _DEPT_JUDGE_RE.search(text)
    if m:
        return m.group("judge_name").strip()
    m2 = _HONORABLE_RE.search(text)
    if m2:
        # HONORABLE format is all-caps; title-case for consistency
        return m2.group("judge_name").strip().title()
    return None


def _sb_courthouse(dept: str) -> str | None:
    """Map a department code to its courthouse name."""
    dept_upper = dept.upper()
    if dept_upper.startswith("S"):
        return "San Bernardino Justice Center"
    if dept_upper.startswith("R"):
        return "Rancho Cucamonga Justice Center"
    return None


class SBTentativeRulingsScraper(PdfLinkScraper):
    """San Bernardino County civil tentative rulings — PDF-link pattern.

    Department is extracted from the PDF filename (link text).
    Judge name is extracted from PDF page 1 text in parse_document(), because
    the SB listing page shows only the filename as link text.
    """

    def __init__(self, config: ScraperConfig, **kwargs: Any) -> None:
        pdf_config = PdfLinkConfig(
            index_url=INDEX_URL,
            pdf_base_url=BASE_URL,
            link_text_re=_LINK_TEXT_RE,
            courthouse_from_dept=_sb_courthouse,
            verify_ssl=True,
            case_number_re=_CASE_NUMBER_RE,
        )
        super().__init__(config, pdf_config=pdf_config, **kwargs)

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract case numbers (via super) and judge name from PDF text."""
        doc = super().parse_document(doc)
        if doc.ruling_text and not doc.judge_name:
            doc.judge_name = _sb_judge_from_pdf_text(doc.ruling_text)
        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-sb-tentatives-civil",
        state="CA",
        county="San Bernardino",
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
