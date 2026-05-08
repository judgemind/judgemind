"""Riverside County Superior Court — Civil Tentative Rulings Scraper (Pattern 2).

Verified against live site 2026-03-02:
  URL:  https://www.riverside.courts.ca.gov/online-services/tentative-rulings
  17 PDF links found on index page (not all current — some from 2023/2024)
  IMPORTANT: site has an SSL certificate issue; verify_ssl=False required.

Link text format: "Department {CODE} - Honorable {Firstname Lastname [Suffix]}"
  e.g. "Department PS1 - Honorable Arthur Hester III"
  e.g. "Department M205 - Honorable Belinda Handy"
  e.g. "Department 01 - Assigned Judge"  (no Honorable prefix)
  e.g. "Department 260"  (no judge suffix at all)

PDF URL pattern: /system/files/{YYYY-MM}/{CODE}ruling{MMDDYY}.pdf
  Resolved against BASE_URL.

PDF structure (multi-ruling, e.g. PS1 with 4 rulings):
  Page 1: "Tentative Rulings for March 2, 2026\\nDepartment PS1\\n..."
    + departmental boilerplate (Zoom call-in, court reporter notice, etc.)
  Case entries are numbered sequentially on a line by themselves and
  followed by 1-3 lines of mixed motion-description and case-number-
  with-parties text, then a ``Tentative Ruling:`` block:

      1.
      Hearing re: Demurrer on 1st Amended
      CVPS2306157 YELDELL vs HENSS Complaint of LACHON YELDELL by
      ISABELE O'KANE
      Tentative Ruling: ...

      2.
      Hearing re: Motion for Terminating
      CVPS2306202 CRUMP vs IRWIN
      Sanctions by JOHN W. IRWIN
      Tentative Ruling: ...

  Case number format: prefix + digits, e.g. "CVPS2306157", "RIC1904113"
  Prefixes: CV + location code (CVPS, CVRI, CVMV, etc.), or court location
  codes (RIC, MCC, PSC, SWC, INC) used by some departments.

Ruling splitting (#3649):
  Multi-ruling Riverside PDFs are split deterministically by the
  ``_split_rulings`` regex helper below.  The ingestion worker hooks
  that splitter into the per-document dispatch loop via
  ``_try_riverside_pdf_split`` in ``ingestion.worker`` so each entry
  gets its own LLM enrichment pass — eliminating the cross-entry
  carry-forward window that the LLM was using to copy outcome /
  motion_type / case_title from one entry onto another.  Single-ruling
  PDFs (the splitter returns ``[]`` or a 1-element list) fall through
  to the framework ``LlmExtractor`` path so the existing per-field
  enrichment fills in motion_type and outcome.

Courthouse mapping (best-effort — Riverside has many locations):
  PS*  → Palm Springs Courthouse
  M*   → Menifee Justice Center
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

# Link text shapes (#2603 — judge suffix is optional):
#   "Department PS1 - Honorable Arthur Hester III"  (standard)
#   "Department 01 - Assigned Judge"                (no Honorable prefix)
#   "Department 260"                                (no dash, no judge)
_LINK_TEXT_RE = re.compile(
    r"Department\s+(?P<department>\S+)"
    r"(?:\s*-\s*(?:Honorable\s+)?(?P<judge_name>.+))?",
    re.IGNORECASE,
)

# Riverside publishes these labels for departments without a permanent judge
# assignment.  The link-text regex captures them as judge_name, but they are
# not real judges — the PDF-text / dept-judge-map fallbacks should populate
# the real name instead.
_PLACEHOLDER_JUDGE_NAMES: frozenset[str] = frozenset({"assigned judge"})


def _is_placeholder_judge(name: str | None) -> bool:
    """Return True iff *name* is a known placeholder, not a real judge name."""
    return bool(name and name.strip().casefold() in _PLACEHOLDER_JUDGE_NAMES)


# Case numbers like "CVPS2306157", "CVRI2412345", "RIC1904113", "MCC2012345"
# CV-prefixed: CV + 2-4 letter location code + 6-8 digits (e.g. CVPS2306157)
# Location-prefixed: RIC, MCC, PSC, SWC, INC + 0-4 letters + 6-10 digits
# Additional prefixes (#2192): CIV (civil), MVC (motor vehicle collision),
#   TEC (Temecula), UDPS (unlawful detainer Palm Springs)
_CASE_NUMBER_RE = re.compile(
    r"\b(?:CV[A-Z]{2,4}|(?:RIC|MCC|PSC|SWC|INC|CIV|MVC|TEC|UDPS)[A-Z]{0,4})\d{6,10}\b",
    re.IGNORECASE,
)


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
# Multi-ruling PDF splitter (#3649)
# ---------------------------------------------------------------------------
#
# Riverside multi-case PDFs use numbered entries that always start with a
# bare ``N.\n`` line at the *start* of a line (no surrounding parens).  The
# body text often contains parenthetical citations like ``(2010) 48 Cal.4th
# 32, 42`` — the split regex must NOT treat those as entry boundaries, so we
# anchor on ``^\d{1,3}\.\s*$`` (number + period on its own line) using
# MULTILINE.  See ``test_riverside_split_*`` tests for ground truth coverage
# against the real fixture PDFs.

# Entry boundary: a 1-3 digit number followed by a period on its own line.
# The negative lookahead for "Cal." / "Cal" guards against false positives
# from California Reporter citations like "Cal.4th 32," that happen to wrap
# such that a digit-period appears at start of line — rare but cheap to
# defend against.  The MULTILINE flag makes ^ / $ match at every line.
_RULING_ENTRY_RE = re.compile(
    r"^(?P<num>\d{1,3})\.\s*$",
    re.MULTILINE,
)


# Tentative ruling body marker.  Riverside PDFs always introduce the
# disposition with ``Tentative Ruling:``; the text before this marker is
# the entry header (motion description + case number + parties), and the
# text after is the body.  Used by ``_split_rulings`` to separate the
# header (which we parse for case_number) from the body (which we keep as
# ``ruling_text`` for the LLM to enrich downstream).
_TENTATIVE_RULING_MARKER_RE = re.compile(
    r"Tentative\s+Ruling:",
    re.IGNORECASE,
)


# Page-N-of-M footer that appears at the bottom of every page.  Strip
# these out of the per-entry body so they don't pollute ``ruling_text``.
_PAGE_FOOTER_RE = re.compile(
    r"\n\s*Page\s+\d+\s+of\s+\d+\s*$",
    re.IGNORECASE,
)


class SplitRuling:
    """A single ruling extracted from a multi-ruling Riverside PDF.

    Mirrors ``courts.ca.fresno_tentatives.SplitRuling`` (#3534) so the
    deterministic splitter dispatch in ``ingestion.worker._try_riverside_pdf_split``
    can produce synthetic split events with the same shape as Fresno.

    Only ``case_number`` and ``ruling_text`` are populated by the splitter;
    motion_type, case_title, and outcome are left to LLM enrichment because
    the Riverside header text wraps unpredictably and a deterministic regex
    extraction is not reliable enough to replace the LLM (#3649).  The
    important property is that each entry's enrichment runs *individually*
    so the LLM cannot carry-forward a previous entry's fields.
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
        hearing_date: datetime | None = None,
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


def _strip_page_footers(text: str) -> str:
    """Remove the trailing 'Page N of M' footer from a per-entry body.

    The PDF text extractor produces these footers as standalone lines at
    every page boundary.  In a multi-page entry the footer can appear
    mid-body too; strip every occurrence anywhere in the entry text.
    """
    # Strip any "Page N of M" line (anywhere in the body).
    cleaned = re.sub(
        r"^\s*Page\s+\d+\s+of\s+\d+\s*$",
        "",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    return cleaned.strip()


def _extract_case_number_from_header(header: str) -> str | None:
    """Pull the case number out of the entry header text.

    The entry header (the text between the ``N.`` boundary and the
    ``Tentative Ruling:`` marker) always contains the case number on a
    line by itself or as the first token of a line — but the surrounding
    text wraps unpredictably.  We use the existing ``_CASE_NUMBER_RE``
    regex (the same one used by the scraper for case-number sanity
    checks) so we benefit from the prefix list maintained alongside
    every other Riverside case-number consumer.
    """
    m = _CASE_NUMBER_RE.search(header)
    return m.group(0).upper() if m else None


def _split_rulings(text: str) -> list[SplitRuling]:
    """Split Riverside multi-ruling PDF text into per-entry ``SplitRuling`` objects.

    The page-1 preamble (Zoom call-in instructions, court reporter notice,
    Local Rule citations) is excluded by anchoring on the first ``^N.\\s*$``
    boundary — anything before the first numbered entry is dropped.

    Returns an empty list if no numbered entries are found, which is the
    expected outcome for "No Tentative Rulings" placeholder PDFs and for
    single-ruling PDFs that don't follow the numbered-entry format.

    Each returned ``SplitRuling`` has:

      * ``ruling_index`` — the integer from the entry header (e.g. ``1``,
        ``2``, ``3`` for a PDF with three rulings).
      * ``case_number`` — extracted via ``_CASE_NUMBER_RE`` from the
        entry header; ``None`` if the regex fails to match (rare on
        well-formed Riverside PDFs).
      * ``ruling_text`` — the **verbatim** entry text from the boundary
        through the next entry boundary (or the end of the document for
        the last entry).  This includes the entry's own ``Tentative
        Ruling:`` body.  We keep it verbatim so downstream LLM enrichment
        sees exactly the same content the LLM would have seen if we'd
        sent the whole PDF — minus the cross-entry contamination that
        causes the carry-forward bug.

    motion_type, case_title, and outcome are left ``None`` on purpose —
    Riverside PDFs do not have the structured field labels that Fresno
    PDFs do (``Motion:`` / ``Re:`` / ``Hearing Date:``), so attempting
    deterministic regex extraction here would produce a high false-
    negative rate.  Letting the framework ``LlmExtractor`` populate
    those fields via per-entry enrichment matches the Fresno fall-
    through path (#3599, AC4 of #3534) and preserves the behaviour the
    LLM was already producing on single-ruling PDFs.
    """
    matches = list(_RULING_ENTRY_RE.finditer(text))
    if not matches:
        return []

    rulings: list[SplitRuling] = []
    for i, match in enumerate(matches):
        try:
            entry_num = int(match.group("num"))
        except (TypeError, ValueError):
            continue

        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

        body = text[start:end].strip()
        if not body:
            continue

        # Defensive: skip anything that looks like a procedural-footer
        # ghost entry (#3649 comment 3).  Real entries always contain a
        # ``Tentative Ruling:`` marker; if the body has none, it's most
        # likely the procedural footer the LLM was previously folding
        # into a fake UNKNOWN-* ruling.
        if not _TENTATIVE_RULING_MARKER_RE.search(body):
            continue

        body = _strip_page_footers(body)
        if len(body) < 30:
            # Too short to be a real ruling — skip.
            continue

        # The header is everything before "Tentative Ruling:"; the body
        # we keep as ruling_text is the whole entry (including the
        # header) so downstream LLM enrichment can see motion type and
        # parties from the header text.
        marker_match = _TENTATIVE_RULING_MARKER_RE.search(body)
        header_text = body[: marker_match.start()] if marker_match else body
        case_number = _extract_case_number_from_header(header_text)

        rulings.append(
            SplitRuling(
                ruling_index=entry_num,
                case_number=case_number,
                ruling_text=body,
            )
        )

    return rulings


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
        return "Menifee Justice Center"
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

            # Reset placeholder judge names captured from link text (#3785).
            # "Assigned Judge" (and variants) are department labels, not real
            # judge names.  Clearing them here lets the PDF-text and
            # dept-judge-map fallbacks below populate the real name.
            if _is_placeholder_judge(doc.judge_name):
                logger.info(
                    "Resetting placeholder judge name from link text",
                    department=doc.department,
                    original=doc.judge_name,
                )
                doc.judge_name = None

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
        # Reset placeholder judge names before attempting fallbacks (#3785).
        if _is_placeholder_judge(doc.judge_name):
            doc.judge_name = None
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


# Extra scraper_ids under which this module's ``_split_rulings`` /
# ``_llm_extract_rulings`` should be registered.  Required so audit / drain
# scripts that key on ``documents.scraper_id`` resolve the splitter on
# rebuild-path rows (rows reconstructed from S3 by ``scripts/rebuild_db.py``,
# which emits ``rebuild-ca-riverside`` instead of the live ``ca-riverside-...``
# id).  See #4331.
_SPLIT_REGISTRY_ALIASES: list[str] = ["rebuild-ca-riverside"]


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
