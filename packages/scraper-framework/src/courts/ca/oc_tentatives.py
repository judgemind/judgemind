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

  Case number availability varies by department, not by courthouse. The
  courthouse-based grouping (Central/West has case numbers, North does
  not) held for some historical PDFs but is not universal — multiple
  Central and West departments now publish PDFs with only entry numbers
  and case titles.

  Formats observed when case numbers are present:
    DD-DDDDDDDD   (e.g. "25-01455183")    — Central/West departments
    DDDD-DDDDDDDD (e.g. "2024-01437598")  — Costa Mesa/Complex, some Central
    Three-part    (e.g. "30-2024-01420730") — normalized to DDDD-DDDDDDDD

  Departments observed with case numbers in PDFs:
    C11, C20, C23, C25, C27, C28, C31, C32, C33, C34, C44, CX*, CM*
  Departments observed without case numbers in PDFs (entry # + title only):
    W8 / W08, N14, N16, N17, N18, C10, C24

  UNKNOWN-prefixed case numbers are the expected fallback for the
  "without" group — not a bug. See #2434 and docs/investigations/
  unknown-case-numbers-oc-riverside-2026-03.md for context.

Courthouse mapping (derived from dept code prefix):
  CX*  -> Complex Civil Department (Laguna Hills)
  C*   -> Central Justice Center (Santa Ana)
  N*   -> North Justice Center (Fullerton)
  W*   -> West Justice Center (Westminster)
  CM*  -> Costa Mesa Justice Center

Field extraction (case_number, hearing_date, case_title, motion_type, outcome,
parties) is handled downstream by the multimodal LLM ingestion pipeline.
The scraper captures raw PDF content and metadata from link text only.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

from framework import CapturedDocument, ScheduleWindow, ScraperConfig

from .pdf_link_scraper import PdfLinkConfig, PdfLinkScraper

logger = structlog.get_logger(__name__)

INDEX_URL = "https://www.occourts.org/online-services/tentative-rulings/civil-tentative-rulings"
BASE_URL = "https://www.occourts.org"

# Link text: "LASTNAME, Firstname [I.] - Dept CX101"
# Also matches "LASTNAME Firstname [I.] - Dept CX105" (comma optional — the
# OC website is inconsistent; e.g. "McCORMICK Melissa R. - Dept CX105").
# Captures last->first separately so we can reconstruct "Firstname Lastname"
_LINK_TEXT_RE = re.compile(
    r"^(?P<last>[A-Z][A-Z\s']+),?\s*(?P<first>[^-]+?)\s*-\s*Dept\.?\s*(?P<department>\S+)",
    re.IGNORECASE,
)


def _oc_link_text_re() -> re.Pattern:
    """OC link text regex with a combined judge_name group for PdfLinkConfig."""
    # PdfLinkConfig expects 'judge_name' and 'department' groups.
    # We post-process in _oc_courthouse to derive judge_name from last+first.
    # Use a wrapper regex that captures all three groups.
    # Comma is optional — the OC website is inconsistent (see #1845).
    return re.compile(
        r"^(?P<judge_name>(?P<last>[A-Z][A-Z\s']+),?\s*(?P<first>[^-]+?))\s*-\s*Dept\.?\s*(?P<department>\S+)",
        re.IGNORECASE,
    )


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
    # C10-C65 etc.
    return "Central Justice Center"


# Matches OC boilerplate PDFs: TENTATIVE RULINGS header with empty Date field
# and no case numbers — these are placeholder PDFs with no actual rulings.
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
            # Some depts use 7-digit sequences (e.g. 24-1377364 in C20)
            # Three-part format (30-2024-01420730) is handled by normalize + this regex
            case_number_re=re.compile(r"\b\d{2,4}-\d{7,8}\b"),
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
        """No-op: field extraction is handled by the multimodal LLM pipeline.

        OC PDFs are archived as raw content during capture. Transcription
        (splitting multi-case documents, extracting ruling text per case)
        and enrichment (case_number, hearing_date, case_title, etc.) are
        performed downstream by the ingestion pipeline using multimodal
        LLM page-image analysis. The scraper does not parse PDF content.
        """
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
