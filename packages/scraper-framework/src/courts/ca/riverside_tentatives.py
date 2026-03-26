"""Riverside County Superior Court — Civil Tentative Rulings Scraper (Pattern 2).

Verified against live site 2026-03-02:
  URL:  https://www.riverside.courts.ca.gov/online-services/tentative-rulings
  17 PDF links found on index page (not all current — some from 2023/2024)
  IMPORTANT: site has an SSL certificate issue; verify_ssl=False required.

Link text format: "Department {CODE} - Honorable {Firstname Lastname [Suffix]}"
  e.g. "Department PS1 - Honorable Arthur Hester III"
  e.g. "Department M205 - Honorable Belinda Handy"

PDF URL pattern: /system/files/{YYYY-MM}/{CODE}ruling{MMDDYY}.pdf
  Resolved against BASE_URL.

PDF structure (PS1, 4 pages):
  Page 1: "Tentative Rulings for March 2, 2026\nDepartment PS1\n..."
  Case entries: "<N>.\\n{CASE_NUMBER} {PARTY_VS_PARTY} {motion}\\nTentative Ruling: ..."
  Case number format: prefix + digits, e.g. "CVPS2306157", "RIC1904113"
  Prefixes: CV + location code (CVPS, CVRI, CVMV, etc.), or court location
  codes (RIC, MCC, PSC, SWC, INC) used by some departments.

Ruling splitting:
  Multi-ruling PDF splitting is handled by the framework-level
  ``LlmExtractor`` in the ingestion worker using a Riverside-specific
  system prompt configured in ``framework.extraction_config`` (#1728).
  The scraper passes whole PDFs through without splitting.

Courthouse mapping (best-effort — Riverside has many locations):
  PS*  → Palm Springs Courthouse
  M*   → Murrieta Courthouse (mid-county)
  MV*  → Moreno Valley Courthouse
  C*   → Corona Courthouse
  01–15 (numbered) → Hall of Justice (Riverside)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import structlog

from framework import CapturedDocument, ScheduleWindow, ScraperConfig
from ingestion.extract import extract_judge_name

from .pdf_link_scraper import PdfLinkConfig, PdfLinkScraper, _extract_pdf_text

logger = structlog.get_logger(__name__)

INDEX_URL = "https://www.riverside.courts.ca.gov/online-services/tentative-rulings"
BASE_URL = "https://www.riverside.courts.ca.gov"

# Link text: "Department PS1 - Honorable Arthur Hester III"
_LINK_TEXT_RE = re.compile(
    r"Department\s+(?P<department>\S+)\s*-\s*Honorable\s+(?P<judge_name>.+)",
    re.IGNORECASE,
)

# Case numbers like "CVPS2306157", "CVRI2412345", "RIC1904113", "MCC2012345"
# CV-prefixed: CV + 2-4 letter location code + 6-8 digits (e.g. CVPS2306157)
# Location-prefixed: RIC, MCC, PSC, SWC, INC + 0-4 letters + 6-10 digits
_CASE_NUMBER_RE = re.compile(r"\b(?:CV[A-Z]{2,4}|(?:RIC|MCC|PSC|SWC|INC)[A-Z]{0,4})\d{6,10}\b")


# Hearing date from PDF text:
#   "Tentative Rulings for March 2, 2026"
#   "No Tentative Rulings March 2, 2026"
_HEARING_DATE_RE = re.compile(
    r"(?:Tentative Rulings\s+(?:for\s+)?|No Tentative Rulings\s+)"
    r"(?P<date>(?:January|February|March|April|May|June|July|August"
    r"|September|October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


def _riv_hearing_date_from_text(text: str) -> datetime | None:
    """Extract hearing date from Riverside PDF text."""
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


_NO_TENTATIVE_RULINGS_RE = re.compile(
    r"^\s*No\s+Tentative\s+Rulings?\b",
    re.IGNORECASE,
)


def _is_no_tentative_rulings(text: str) -> bool:
    """Return True if the PDF text is a 'No Tentative Rulings' boilerplate page.

    Riverside publishes placeholder PDFs for departments with no rulings.
    These pages start with 'No Tentative Rulings' followed by a date or
    'for' and then standard boilerplate about court reporters. They should
    not be ingested as actual ruling records.

    .. note:: This is now also handled by the base-class ``_is_boilerplate()``
       hook in ``PdfLinkScraper``, which uses the same regex.  This function
       is kept for backward compatibility with existing tests.
    """
    return bool(_NO_TENTATIVE_RULINGS_RE.match(text))


# ---------------------------------------------------------------------------
# Courthouse mapping
# ---------------------------------------------------------------------------


def _riv_courthouse(dept: str) -> str | None:
    dept_upper = dept.upper()
    if dept_upper.startswith("PS"):
        return "Palm Springs Courthouse"
    if dept_upper.startswith("MV"):
        return "Moreno Valley Courthouse"
    if dept_upper.startswith("M"):
        return "Murrieta Courthouse"
    if dept_upper.startswith("C"):
        return "Corona Courthouse"
    # Numbered departments (01–15): Hall of Justice in Riverside
    if dept_upper.isdigit() or dept_upper.lstrip("0").isdigit():
        return "Hall of Justice"
    return None


class RiversideTentativeRulingsScraper(PdfLinkScraper):
    """Riverside County civil tentative rulings — PDF-link pattern.

    Passes whole PDFs through without splitting.  Multi-ruling PDF
    splitting is handled downstream by the ingestion worker using the
    framework ``LlmExtractor`` with a Riverside-specific system prompt
    configured in ``framework.extraction_config`` (#1728).
    """

    def __init__(
        self,
        config: ScraperConfig,
        dept_judge_map: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        pdf_config = PdfLinkConfig(
            index_url=INDEX_URL,
            pdf_base_url=BASE_URL,
            link_text_re=_LINK_TEXT_RE,
            courthouse_from_dept=_riv_courthouse,
            verify_ssl=False,  # Riverside has a bad TLS cert on the live site
            case_number_re=_CASE_NUMBER_RE,
        )
        super().__init__(config, pdf_config=pdf_config, **kwargs)
        self._dept_judge_map: dict[str, str] = dept_judge_map or {}

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch PDFs and enrich with judge name from PDF content.

        Boilerplate "No Tentative Rulings" PDFs are already filtered by the
        base-class ``_is_boilerplate()`` hook before reaching this method.

        Multi-ruling splitting is NOT done here — the ingestion worker
        handles it via the framework ``LlmExtractor`` (#1728).
        """
        raw_docs = super().fetch_documents()

        for doc in raw_docs:
            try:
                text = _extract_pdf_text(doc.raw_content)
            except Exception as exc:
                logger.warning("PDF text extraction failed", error=str(exc))
                continue

            # Fallback: extract judge name from PDF text when link text
            # didn't provide one (#411).  Many Riverside PDFs contain
            # "Department X - Honorable Name" headers in the body text.
            if not doc.judge_name:
                pdf_judge = extract_judge_name(text)
                if pdf_judge:
                    doc.judge_name = pdf_judge
                    logger.info(
                        "Extracted judge name from PDF content",
                        department=doc.department,
                        judge=pdf_judge,
                    )

            # Fallback: use department-to-judge mapping (#585)
            if not doc.judge_name and doc.department and self._dept_judge_map:
                from courts.ca.riverside_dept_judges import lookup_judge_for_department

                mapped_name = lookup_judge_for_department(self._dept_judge_map, doc.department)
                if mapped_name:
                    doc.judge_name = mapped_name
                    logger.info(
                        "Judge name populated from department mapping",
                        department=doc.department,
                        judge_name=mapped_name,
                    )

        return raw_docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract fields from PDF text.

        Uses the parent class parse logic to extract ruling_text from
        the PDF.  Adds hearing date extraction and judge name fallback.
        """
        doc = super().parse_document(doc)
        if doc.ruling_text and not doc.hearing_date:
            doc.hearing_date = _riv_hearing_date_from_text(doc.ruling_text)
        # Fallback: extract judge name from ruling text (#411)
        if not doc.judge_name and doc.ruling_text:
            pdf_judge = extract_judge_name(doc.ruling_text)
            if pdf_judge:
                doc.judge_name = pdf_judge
        # Fallback: department-to-judge mapping (#585)
        if not doc.judge_name and doc.department and self._dept_judge_map:
            from courts.ca.riverside_dept_judges import lookup_judge_for_department

            mapped_name = lookup_judge_for_department(self._dept_judge_map, doc.department)
            if mapped_name:
                doc.judge_name = mapped_name
        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-riverside-tentatives-civil",
        state="CA",
        county="Riverside",
        court="Superior Court",
        target_urls=[INDEX_URL],
        poll_interval_seconds=43200,
        schedule_windows=[
            ScheduleWindow(start=dtime(15, 0), end=dtime(16, 0)),  # 3 PM sweep
            ScheduleWindow(start=dtime(21, 0), end=dtime(22, 0)),  # 9 PM catch-up
        ],
        request_delay_seconds=1.0,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
