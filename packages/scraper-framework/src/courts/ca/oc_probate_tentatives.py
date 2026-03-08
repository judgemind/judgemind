"""Orange County Superior Court — Probate Tentative Rulings Scraper (Pattern 2).

Verified against live site 2026-03-07:
  URL:  https://www.occourts.org/online-services/tentative-rulings/probate-tentative-rulings
  6 PDF links found on index page

Link text format: "CMN Law and Motion" (department code + description)
  e.g. "CM3 Law and Motion", "CM4 Law and Motion"
  No judge name in link text — extracted from PDF.

PDF URL pattern: https://www.occourts.org/sites/default/files/oc/default/tentative-rulings/CM{N}rulings.pdf

PDF structure (CM3 / Judge Erin Rowe, 6 pages):
  Header: "Superior Court of the State of California / County of Orange"
  "TENTATIVE RULINGS FOR DEPARTMENT CM3"
  "HON. Judge Erin Rowe"
  "Date: 03/04/26"
  Case rows:
    "# Case Name Tentative"
    "<line#> <CaseName - Type>"
    "<case_number> <MOTION TYPE>"
    Then ruling text.
  Case number format: 0DDDDDDD (8 digits starting with 0, e.g. "01157766")

All probate departments are CM* → Costa Mesa Justice Center.

Investigation: #142
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from framework import CapturedDocument, ScheduleWindow, ScraperConfig

from .pdf_link_scraper import PdfLinkConfig, PdfLinkScraper

INDEX_URL = "https://www.occourts.org/online-services/tentative-rulings/probate-tentative-rulings"
BASE_URL = "https://www.occourts.org"

# Link text: "CM3 Law and Motion" — extract department code
_LINK_TEXT_RE = re.compile(
    r"^(?P<department>CM\d+)\s+(?P<judge_name>Law\s+and\s+Motion)",
    re.IGNORECASE,
)

# Judge from PDF text: "HON. Judge Erin Rowe" or "HON. Commissioner ..."
_JUDGE_RE = re.compile(
    r"HON\.\s+(?:Judge|Commissioner)\s+(?P<judge_name>[^\n]+)",
    re.IGNORECASE,
)

# Hearing date from PDF: "Date: 03/04/26" (MM/DD/YY or MM/DD/YYYY)
_PDF_DATE_RE = re.compile(r"Date:\s*(?P<date>\d{2}/\d{2}/\d{2,4})")

# Case number: 8 digits starting with 0 (e.g. "01157766")
_CASE_NUMBER_RE = re.compile(r"\b0\d{7}\b")

# Case title from probate: "Fard - Trust", "Collins – Trust", "McCoy – Elder Abuse"
# Pattern: "# <CaseTitle>" at start of case block, may be followed by
# "TENTATIVE" on same line (CM5 style) or newline with case number (CM3 style).
_CASE_TITLE_RE = re.compile(
    r"^\d+\s+(?P<title>[A-Za-z][^\n]+?)(?:\s+(?:TENTATIVE|Tentative)|\s*$)",
    re.MULTILINE,
)

# Motion type from probate PDFs — these are explicit
_MOTION_TYPE_RE = re.compile(
    r"\b(MOTION[S]?\s+(?:FOR|TO)\s+[^\n(]+)",
    re.IGNORECASE,
)

# Outcome keywords
_OUTCOME_RE = re.compile(
    r"\b(GRANTED|DENIED|CONTINUED|MOOT|OFF\s+CALENDAR)\b",
    re.IGNORECASE,
)

# Department from PDF text: "TENTATIVE RULINGS FOR DEPARTMENT CM3"
_PDF_DEPT_RE = re.compile(
    r"DEPARTMENT\s+(?P<department>CM\d+)",
    re.IGNORECASE,
)


def _probate_judge_from_text(text: str) -> str | None:
    """Extract judge name from probate PDF text."""
    m = _JUDGE_RE.search(text)
    if m:
        raw = m.group("judge_name").strip()
        return raw.title() if raw.isupper() else raw
    return None


def _probate_hearing_date_from_text(text: str) -> datetime | None:
    """Extract hearing date from probate PDF (MM/DD/YY or MM/DD/YYYY format)."""
    m = _PDF_DATE_RE.search(text)
    if not m:
        return None
    raw = m.group("date")
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _probate_case_title_from_text(text: str) -> str | None:
    """Extract the first case title from probate PDF text."""
    m = _CASE_TITLE_RE.search(text)
    if m:
        raw = m.group("title").strip()
        # Clean up em-dashes and normalize whitespace
        raw = raw.replace("\u2013", "-").replace("\u2014", "-")
        return " ".join(raw.split())
    return None


def _probate_outcome_from_text(text: str) -> str | None:
    """Extract the first outcome keyword from probate PDF text."""
    m = _OUTCOME_RE.search(text)
    if m:
        return m.group(1).strip().upper()
    return None


def _probate_motion_type_from_text(text: str) -> str | None:
    """Extract motion type from probate PDF text."""
    m = _MOTION_TYPE_RE.search(text)
    if m:
        raw = m.group(1).strip()
        # Clean up parenthetical references
        raw = re.sub(r"\s*\(ROA.*", "", raw)
        return " ".join(raw.split())[:100]
    return None


def _oc_probate_courthouse(_dept: str) -> str:
    """All probate departments are in the Costa Mesa Justice Center."""
    return "Costa Mesa Justice Center"


class OCProbateTentativeRulingsScraper(PdfLinkScraper):
    """Orange County Probate tentative rulings — PDF-link pattern.

    Department is extracted from link text (e.g. "CM3 Law and Motion").
    Judge name is extracted from PDF text ("HON. Judge ...") in parse_document().
    """

    def __init__(self, config: ScraperConfig, **kwargs: Any) -> None:
        pdf_config = PdfLinkConfig(
            index_url=INDEX_URL,
            pdf_base_url=BASE_URL,
            link_text_re=_LINK_TEXT_RE,
            courthouse_from_dept=_oc_probate_courthouse,
            verify_ssl=True,
            case_number_re=_CASE_NUMBER_RE,
        )
        super().__init__(config, pdf_config=pdf_config, **kwargs)

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract all fields from probate PDF text."""
        doc = super().parse_document(doc)

        if doc.ruling_text:
            # Judge name from PDF text (not in link text for probate).
            # The link text captures "Law and Motion" as judge_name placeholder —
            # always replace it with the real judge name from the PDF.
            if not doc.judge_name or "law and motion" in doc.judge_name.lower():
                doc.judge_name = _probate_judge_from_text(doc.ruling_text)

            # Department from PDF text (may be more accurate than link text)
            pdf_dept_match = _PDF_DEPT_RE.search(doc.ruling_text)
            if pdf_dept_match:
                doc.department = pdf_dept_match.group("department").upper()

            # Hearing date
            if not doc.hearing_date:
                doc.hearing_date = _probate_hearing_date_from_text(doc.ruling_text)

            # Case title
            if not doc.case_title:
                doc.case_title = _probate_case_title_from_text(doc.ruling_text)

            # Outcome
            if not doc.outcome:
                doc.outcome = _probate_outcome_from_text(doc.ruling_text)

            # Motion type
            if not doc.motion_type:
                doc.motion_type = _probate_motion_type_from_text(doc.ruling_text)

        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default OC Probate scraper configuration."""
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-oc-tentatives-probate",
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
