"""San Francisco Superior Court — Family Law Tentative Rulings Scraper (Pattern 2).

Verified against fixture captured 2026-03-03:
  URL:  https://webapps.sftc.org/ufctr/ufctr.dll
  19 PDF links found on index page (across Depts 403, 404, 416)
  No CAPTCHA; httpx fetch works directly.

Link text format: filename only — e.g. "403 Tentative Rulings 3.03.2026.pdf"
  No judge name in link text; extracted from PDF text.
  Department extracted from filename (leading digits before space).

PDF structure (pages 3+):
  Header block per ruling:
    Case Number: FPT-25-378624
    Hearing Date: March 3, 2026
    Department: 403
    Presiding: BOBBY P. LUNA

Case number format: F + 2 uppercase letters + hyphen + 2-digit year + hyphen + 6 digits
  e.g. FPT-25-378624, FMS-20-387302, FDI-14-781786

Departments: 403, 404, 416
Calendar days: Tuesday and Thursday
Previous rulings available for ~30 days, auto-deleted.

Investigation: #9
Report: docs/investigations/sf-tentative-rulings-2026-03.md
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from framework import CapturedDocument, ScheduleWindow, ScraperConfig

from .pdf_link_scraper import PdfLinkConfig, PdfLinkScraper

INDEX_URL = "https://webapps.sftc.org/ufctr/ufctr.dll"
BASE_URL = "https://webapps.sftc.org/ufctr/"

# Link text = filename: "403 Tentative Rulings 3.03.2026.pdf"
# Extract department from leading digits.
_LINK_TEXT_RE = re.compile(r"^(?P<department>\d{3})\s+Tentative\s+Rulings")

# Hearing date from filename: "403 Tentative Rulings 3.03.2026.pdf" → M.DD.YYYY or MM.DD.YYYY
_FILENAME_DATE_RE = re.compile(r"(\d{1,2})\.(\d{2})\.(\d{4})\.pdf$")

# Judge name from PDF text: "Presiding: BOBBY P. LUNA" (may have trailing whitespace)
_PRESIDING_RE = re.compile(
    r"Presiding:\s+(?P<judge_name>[A-Z][^\n]+)",
    re.IGNORECASE,
)

# Case number format: F + 2 uppercase letters + hyphen + 2-digit year + hyphen + 6 digits
_CASE_NUMBER_RE = re.compile(r"\bF[A-Z]{2}-\d{2}-\d{6}\b")


def _sf_judge_from_pdf_text(text: str) -> str | None:
    """Extract judge name from SF PDF text (e.g. 'BOBBY P. LUNA' → 'Bobby P. Luna')."""
    m = _PRESIDING_RE.search(text)
    if m:
        raw = m.group("judge_name").strip()
        # Remove trailing parentheses or whitespace artifacts
        raw = raw.rstrip(")")
        return raw.title() if raw.isupper() else raw
    return None


def _sf_hearing_date_from_filename(filename: str) -> datetime | None:
    """Parse hearing date from filename like '403 Tentative Rulings 3.03.2026.pdf'."""
    m = _FILENAME_DATE_RE.search(filename)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime(year, month, day)
    return None


def _sf_courthouse(_dept: str) -> str | None:
    """Map a department code to its courthouse name.

    All SF Family Law departments are in the same courthouse.
    """
    return "San Francisco Courthouse"


class SFTentativeRulingsScraper(PdfLinkScraper):
    """San Francisco Family Law tentative rulings — PDF-link pattern.

    Department is extracted from the PDF filename (link text).
    Judge name is extracted from PDF page 1 text in parse_document(), because
    the SF listing page shows only the filename as link text.
    """

    def __init__(self, config: ScraperConfig, **kwargs: Any) -> None:
        pdf_config = PdfLinkConfig(
            index_url=INDEX_URL,
            pdf_base_url=BASE_URL,
            link_text_re=_LINK_TEXT_RE,
            courthouse_from_dept=_sf_courthouse,
            verify_ssl=True,
            case_number_re=_CASE_NUMBER_RE,
        )
        super().__init__(config, pdf_config=pdf_config, **kwargs)

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract case numbers (via super) and judge name + hearing date from PDF text."""
        doc = super().parse_document(doc)

        # Extract judge name from PDF text if not already set
        if doc.ruling_text and not doc.judge_name:
            doc.judge_name = _sf_judge_from_pdf_text(doc.ruling_text)

        # Extract hearing date from link text (filename) stored in extra
        link_text = doc.extra.get("link_text", "")
        if link_text and not doc.hearing_date:
            doc.hearing_date = _sf_hearing_date_from_filename(link_text)

        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default SF Family Law scraper configuration."""
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-sf-tentatives-family-law",
        state="CA",
        county="San Francisco",
        court="Superior Court",
        target_urls=[INDEX_URL],
        poll_interval_seconds=43200,  # twice daily
        schedule_windows=[
            ScheduleWindow(start=dtime(14, 0), end=dtime(15, 0)),  # 2 PM sweep
            ScheduleWindow(start=dtime(21, 0), end=dtime(22, 0)),  # 9 PM catch-up
        ],
        request_delay_seconds=1.0,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
