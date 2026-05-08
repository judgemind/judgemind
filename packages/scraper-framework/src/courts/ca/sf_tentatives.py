"""San Francisco Superior Court — Family Law Tentative Rulings Scraper (Pattern 2).

Verified against fixture captured 2026-03-03:
  URL:  https://webapps.sftc.org/ufctr/ufctr.dll
  19 PDF links found on index page (across Depts 403, 404, 416;
     site may also list 405, 405A, 406, 425)
  No CAPTCHA; httpx fetch works directly.

Link text format: filename only — e.g. "403 Tentative Rulings 3.03.2026.pdf"
  No judge name in link text; extracted from PDF text.
  Department extracted from filename (leading digits before space).

PDF structure (pages 3+, multi-case calendars):
  Each entry begins with a ``SUPERIOR COURT OF CALIFORNIA`` page header
  followed within ~10 lines by a multi-line caption block carrying
  ``Case Number: FPT-25-378624``, ``Hearing Date: ...``, ``Department: ...``,
  ``Presiding: BOBBY P. LUNA`` on adjacent lines (line numbers + ``)``
  delimiters interleave the labels and values).

Case number format: F + 2 uppercase letters + hyphen + 2-digit year + hyphen + 6 digits
  e.g. FPT-25-378624, FMS-20-387302, FDI-14-781786

Departments: 403, 404, 405, 405A, 406, 416, 425
Calendar days: Tuesday and Thursday
Previous rulings available for ~30 days, auto-deleted.

Multi-case PDF splitting (#4304):
  SF family-law PDFs commonly bundle 5–15 distinct rulings in one file.
  Sending the whole PDF through the framework ``LlmExtractor`` lets the
  LLM violate rule 5b of its own prompt and copy the first entry's
  ``case_title`` (and other LLM-extracted fields) onto every subsequent
  entry — producing the ``all_same_case_title_cluster`` pattern flagged
  by the cross-county audit (#4289).  The ``_split_rulings`` helper
  below splits a multi-case PDF into per-entry ``SplitRuling`` objects
  using the ``SUPERIOR COURT OF CALIFORNIA`` page header as the entry
  boundary; the ingestion worker hooks the splitter into per-document
  dispatch via ``_try_sf_pdf_split`` in ``ingestion.worker`` so each
  entry gets its own LLM enrichment pass.  This mirrors the Riverside
  pattern (#3649) and Fresno pattern (#3534) — same fix family, same
  shape.  Single-ruling PDFs (the splitter returns ``[]`` or a 1-element
  list) fall through to the framework ``LlmExtractor`` path so the
  existing per-field enrichment fills in motion_type and outcome.

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
# Extract department from leading digits, with optional letter suffix (e.g. 405A).
_LINK_TEXT_RE = re.compile(r"^(?P<department>\d{3}[A-Z]?)\s+Tentative\s+Rulings")

# Hearing date from filename: "403 Tentative Rulings 3.03.2026.pdf" → M.DD.YYYY or MM.DD.YYYY
_FILENAME_DATE_RE = re.compile(r"(\d{1,2})\.(\d{2})\.(\d{4})\.pdf$")

# Judge name from PDF text: "Presiding: BOBBY P. LUNA" (may have trailing whitespace)
_PRESIDING_RE = re.compile(
    r"Presiding:\s+(?P<judge_name>[A-Z][^\n]+)",
    re.IGNORECASE,
)

# Case number format: F + 2 uppercase letters + hyphen + 2-digit year + hyphen + 6 digits
_CASE_NUMBER_RE = re.compile(r"\bF[A-Z]{2}-\d{2}-\d{6}\b")

# Multi-case entry boundary (#4304).  Each ruling in a multi-case SF
# family-law PDF starts on its own page with the standard court header
# ``SUPERIOR COURT OF CALIFORNIA``.  Anchored at start-of-line and
# tolerating an optional leading line-number prefix (``"1 "``) inserted
# by some PDF text extractors.  Page-1 / page-2 of a multi-case PDF carry
# Tentative Ruling Instructions and Zoom-call boilerplate without this
# header, so the regex naturally skips the preamble.
_ENTRY_HEADER_RE = re.compile(
    r"^\s*\d*\s*SUPERIOR\s+COURT\s+OF\s+CALIFORNIA\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Caption-block ``Case Number:`` extractor used by ``_split_rulings`` to
# attach a deterministic case_number to each split entry.  Limited to the
# F-prefix family-law shape so it does not match unrelated case-number-
# like substrings inside the body (e.g. references to civil case numbers).
_CASE_NUMBER_CAPTION_RE = re.compile(
    r"Case\s+Number:\s*(F[A-Z]{2}-\d{2}-\d{6})",
    re.IGNORECASE,
)

# Caption-block ``Department:`` extractor (3-digit dept, optional letter
# suffix mirrors the link-text shape in ``_LINK_TEXT_RE``).
_DEPARTMENT_CAPTION_RE = re.compile(
    r"Department:\s*(?P<department>\d{3}[A-Z]?)",
    re.IGNORECASE,
)


class SplitRuling:
    """A single ruling extracted from a multi-ruling SF family-law PDF (#4304).

    Mirrors ``courts.ca.riverside_tentatives.SplitRuling`` (#3649) and
    ``courts.ca.fresno_tentatives.SplitRuling`` (#3534) so the deterministic
    splitter dispatch in ``ingestion.worker._try_sf_pdf_split`` can produce
    synthetic split events with the same shape as those counties.

    Only ``case_number`` and ``ruling_text`` are populated by the splitter;
    motion_type, case_title, and outcome are left to LLM enrichment because
    the SF caption header wraps unpredictably (multi-line petitioner names,
    ``)`` delimiters, line-number prefixes from the PDF text extractor) and
    a deterministic regex extraction is not reliable enough to replace the
    LLM.  The important property is that each entry's enrichment runs
    *individually* so the LLM cannot carry-forward a previous entry's
    fields onto the next one.
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


def _split_rulings(text: str) -> list[SplitRuling]:
    """Split SF family-law multi-ruling PDF text into per-entry ``SplitRuling`` objects.

    The page-1 / page-2 preamble (Tentative Ruling Instructions, Zoom
    call-in boilerplate, courthouse contact info) is excluded by anchoring
    on ``SUPERIOR COURT OF CALIFORNIA`` page headers — the preamble pages
    do not carry that header.  Entries without a ``Case Number:`` line in
    their caption block (e.g. an interstitial blank page that happens to
    carry the court header) are dropped to avoid synthesizing ghost
    rulings.

    Returns an empty list if no entry headers are found, which is the
    expected outcome for very short single-page PDFs that don't follow
    the multi-case calendar format.

    Each returned ``SplitRuling`` has:

      * ``ruling_index`` — the zero-based index of the entry within the
        PDF (entry 0 is the first ruling, not the preamble).
      * ``case_number`` — extracted via ``_CASE_NUMBER_CAPTION_RE`` from
        the entry's caption block; ``None`` if the regex fails to match
        (rare on well-formed SF PDFs but defensive).
      * ``department`` — extracted via ``_DEPARTMENT_CAPTION_RE`` from
        the entry's caption block; falls back to ``None`` if absent.
      * ``ruling_text`` — the **verbatim** entry text from the boundary
        through the next entry boundary (or the end of the document for
        the last entry).  This includes the entry's own caption block
        and tentative ruling body.  We keep it verbatim so downstream
        LLM enrichment sees exactly the same content the LLM would have
        seen if we'd sent the whole PDF — minus the cross-entry
        contamination that causes the carry-forward bug.

    motion_type, case_title, and outcome are left ``None`` on purpose —
    the SF caption block doesn't have structured field labels for those
    (the case title is reconstructed from a multi-line VS. block elsewhere
    in this module via ``_sf_case_title_from_pdf_text`` for the
    single-PDF parse path, but downstream LLM enrichment is what fills
    those fields on the split path).  Letting the framework
    ``LlmExtractor`` populate those fields via per-entry enrichment
    matches the Riverside fall-through pattern (#3649) and preserves
    the behaviour the LLM was already producing on single-ruling PDFs.
    """
    matches = list(_ENTRY_HEADER_RE.finditer(text))
    if not matches:
        return []

    rulings: list[SplitRuling] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

        body = text[start:end].strip()
        if not body:
            continue

        # Real ruling entries always carry a Case Number line in the
        # caption block.  Skip header-only / blank-page interstitials so
        # we don't synthesize ghost rulings.
        cn_match = _CASE_NUMBER_CAPTION_RE.search(body)
        if not cn_match:
            continue

        case_number = cn_match.group(1).upper()

        dept_match = _DEPARTMENT_CAPTION_RE.search(body)
        department = dept_match.group("department") if dept_match else None

        rulings.append(
            SplitRuling(
                ruling_index=len(rulings),
                case_number=case_number,
                ruling_text=body,
                department=department,
            )
        )

    return rulings


# SF court caption format (multi-line, with line numbers and ")" delimiters):
#
#   6 MICHAEL EDWARD GRAVES, ) Case Number: FPT-25-378624
#   )
#   7 Petitioner ) Hearing Date: March 3, 2026
#   )
#   8 VS. ) Hearing Time: 9:00 AM
#   )
#   9 RANJIE LONG, ) Department: 403
#   )
#  10 Respondent ) Presiding: BOBBY P. LUNA
#
# Petitioner name may span multiple lines:
#   6 MARIA DE LOS ANGELES RAMIREZ ) Case Number: FPT-25-378672
#   )
#   7 HERNANDEZ, ) Hearing Date: March 3, 2026
#   )
#   8 Petitioner ) Hearing Time: 9:00 AM
#
# Strategy: find "VS." line, scan backward for petitioner, forward for respondent.

# Metadata fields that appear after ")" on caption lines — used to strip noise.
_CAPTION_METADATA_RE = re.compile(
    r"\)\s*(?:Case\s+Number:|Hearing\s+(?:Date|Time):|Department:|Presiding:).*",
    re.IGNORECASE,
)

# Leading line-number prefix: "6 " or "10 " at start of line
_LINE_NUM_RE = re.compile(r"^\s*\d+\s+")


def _clean_caption_line(line: str) -> str:
    """Strip metadata, line numbers, and delimiter noise from a caption line."""
    # Remove metadata after ")"
    line = _CAPTION_METADATA_RE.sub("", line)
    # Remove leading line number
    line = _LINE_NUM_RE.sub("", line)
    # Remove remaining ")" characters and whitespace
    line = line.replace(")", "").strip()
    # Remove trailing/leading commas
    line = line.strip(",").strip()
    # Reject bare line numbers (e.g. "4", "29") that weren't caught above
    if line.isdigit():
        return ""
    return line


def _sf_case_title_from_pdf_text(text: str) -> str | None:
    """Extract case title from SF PDF caption block.

    Parses the multi-line court caption format used by SF Family Court PDFs,
    extracting petitioner and respondent names to form "Petitioner v. Respondent".
    """
    lines = text.split("\n")

    # Find lines containing "VS." (the separator between petitioner/respondent)
    vs_indices = [i for i, line in enumerate(lines) if re.search(r"\bVS\.\s*\)", line)]
    if not vs_indices:
        return None

    vs_idx = vs_indices[0]

    # --- Extract petitioner name ---
    # Scan backward from VS. line to find name lines before "Petitioner"
    pet_parts: list[str] = []
    for i in range(vs_idx - 1, max(vs_idx - 10, -1), -1):
        cleaned = _clean_caption_line(lines[i])
        if not cleaned:
            continue
        upper = cleaned.upper()
        if upper == "PETITIONER":
            continue  # skip the role marker line
        # Stop at court header lines or empty structural lines
        if any(kw in upper for kw in ("SUPERIOR COURT", "COUNTY OF", "UNIFIED FAMILY", "COURT")):
            break
        pet_parts.insert(0, cleaned)

    # --- Extract respondent name ---
    # Scan forward from VS. line to find name lines before "Respondent"
    resp_parts: list[str] = []
    for i in range(vs_idx + 1, min(vs_idx + 10, len(lines))):
        cleaned = _clean_caption_line(lines[i])
        if not cleaned:
            continue
        upper = cleaned.upper()
        if upper == "RESPONDENT":
            break  # stop at the role marker
        if "REQUEST FOR ORDER" in upper or "TENTATIVE RULING" in upper:
            break  # went too far
        resp_parts.append(cleaned)

    pet_name = " ".join(pet_parts).strip()
    resp_name = " ".join(resp_parts).strip()

    if not pet_name or not resp_name:
        return None

    # Title-case if all uppercase
    if pet_name.isupper():
        pet_name = pet_name.title()
    if resp_name.isupper():
        resp_name = resp_name.title()

    return f"{pet_name} v. {resp_name}"


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
        """Extract case numbers, judge name, hearing date, and case title."""
        doc = super().parse_document(doc)

        # Extract judge name from PDF text if not already set
        if doc.ruling_text and not doc.judge_name:
            doc.judge_name = _sf_judge_from_pdf_text(doc.ruling_text)

        # Extract hearing date from link text (filename) stored in extra
        link_text = doc.extra.get("link_text", "")
        if link_text and not doc.hearing_date:
            doc.hearing_date = _sf_hearing_date_from_filename(link_text)

        # Extract case title from SF caption block
        if doc.ruling_text and not doc.case_title:
            doc.case_title = _sf_case_title_from_pdf_text(doc.ruling_text)

        return doc


# Extra scraper_ids under which this module's ``_split_rulings`` /
# ``_llm_extract_rulings`` should be registered.  Required so audit / drain
# scripts that key on ``documents.scraper_id`` resolve the splitter on
# rebuild-path rows (rows reconstructed from S3 by ``scripts/rebuild_db.py``,
# which emits ``rebuild-ca-san_francisco`` instead of the live ``ca-sf-...`` id).
# See #4331.
_SPLIT_REGISTRY_ALIASES: list[str] = ["rebuild-ca-san_francisco"]


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
