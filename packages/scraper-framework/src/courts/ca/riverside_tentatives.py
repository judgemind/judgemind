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
  Case entries: "<N>.\n{CASE_NUMBER} {PARTY_VS_PARTY} {motion}\nTentative Ruling: ..."
  Case number format: prefix + digits, e.g. "CVPS2306157", "RIC1904113"
  Prefixes: CV + location code (CVPS, CVRI, CVMV, etc.), or court location
  codes (RIC, MCC, PSC, SWC, INC) used by some departments.

Ruling splitting:
  A single PDF may contain rulings for multiple cases, each starting with a numbered
  entry (e.g. "1.\\n...CVPS2306157..."). We split on these numbered boundaries so each
  case gets its own CapturedDocument with correct case number, ruling text, parties,
  motion type, and outcome.

Courthouse mapping (best-effort — Riverside has many locations):
  PS*  → Palm Springs Courthouse
  M*   → Murrieta Courthouse (mid-county)
  MV*  → Moreno Valley Courthouse
  C*   → Corona Courthouse
  01–15 (numbered) → Hall of Justice (Riverside)
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Any

import structlog

from framework import CapturedDocument, ContentFormat, ScheduleWindow, ScraperConfig
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


# ---------------------------------------------------------------------------
# Ruling-splitting helpers
# ---------------------------------------------------------------------------

# Numbered ruling entry: "1.\n..." or "1.\nCVPS2306157 ..."
# Matches a line that is just a number followed by a period, at a line boundary.
# The number can be at the start of a line or after a page break.
_RULING_ENTRY_RE = re.compile(
    r"^(?P<num>\d{1,3})\.\s*$",
    re.MULTILINE,
)

# Party pattern: "YELDELL vs HENSS" or "BANK OF AMERICA, N.A. vs VARGAS"
# Captures plaintiff and defendant around " vs " (case-insensitive).
_PARTY_VS_RE = re.compile(
    r"^(?P<plaintiff>.+?)\s+vs\s+(?P<defendant>.+?)$",
    re.IGNORECASE | re.MULTILINE,
)

# Outcome from "Tentative Ruling:" line — extract first sentence or keyword.
_OUTCOME_RE = re.compile(
    r"Tentative Ruling:\s*(?P<outcome>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)


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


class SplitRuling:
    """A single ruling extracted from a multi-ruling PDF."""

    __slots__ = (
        "ruling_index",
        "case_number",
        "ruling_text",
        "case_title",
        "motion_type",
        "outcome",
    )

    def __init__(
        self,
        ruling_index: int,
        case_number: str | None,
        ruling_text: str,
        case_title: str | None,
        motion_type: str | None,
        outcome: str | None,
    ) -> None:
        self.ruling_index = ruling_index
        self.case_number = case_number
        self.ruling_text = ruling_text
        self.case_title = case_title
        self.motion_type = motion_type
        self.outcome = outcome


def _filter_entry_matches(
    matches: list[re.Match[str]],
    text: str,
) -> list[re.Match[str]]:
    """Filter regex matches to only keep real ruling entry markers.

    Riverside PDFs number their rulings sequentially (1, 2, 3, ...).
    However, the ruling *body* text sometimes contains numbered points
    (e.g., "The court finds:\\n1.\\nThe motion is granted...") that also
    match the ``_RULING_ENTRY_RE`` pattern.  These spurious matches cause
    ruling text to be assigned to the wrong case (#1410, #1716).

    Strategy (three passes):

    **Pass 1** — keep only matches whose text block contains a Riverside
    case number.  This eliminates most spurious matches (body-text
    numbered points that don't mention any case number).

    **Pass 2** — group surviving candidates by entry number and pick the
    best match for each number.  When there are *duplicate* matches for
    the same entry number (e.g. two "2." matches — one from body text
    and one real entry), prefer the match whose block also contains
    "Tentative Ruling:" (#1716).  This handles the case where a long
    ruling's body text has numbered analytical paragraphs that reference
    case numbers (e.g. "1.\\nMotion re CVRI2403055:..."), which pass the
    case-number-only check but are not real entry markers.

    **Pass 3** — verify the selected matches form a consecutive 1-based
    sequence and are in ascending positional order.  Any gap causes the
    function to stop — better to return fewer rulings than to mis-assign
    text.
    """
    if not matches:
        return []

    # Pass 1: keep only matches whose text block contains a case number.
    # Also record whether the block contains "Tentative Ruling:" for use
    # as a quality signal in Pass 2.
    kept: list[tuple[re.Match[str], int, bool]] = []  # (match, idx, has_tentative)
    for i, m in enumerate(matches):
        block_start = m.end()
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[block_start:block_end]
        if _CASE_NUMBER_RE.search(block):
            has_tentative = "tentative ruling" in block.lower()
            kept.append((m, i, has_tentative))
        else:
            logger.debug(
                "Skipping spurious entry match",
                entry_num=m.group("num"),
                position=m.start(),
            )

    if not kept:
        return []

    # Pass 2: group by entry number and pick the best candidate for each.
    # Prefer matches whose block contains "Tentative Ruling:".  Among
    # equally-qualified matches, prefer the first (lowest position).
    by_num: dict[int, list[tuple[re.Match[str], bool]]] = defaultdict(list)
    for m, _idx, has_tentative in kept:
        num = int(m.group("num"))
        by_num[num].append((m, has_tentative))

    best: dict[int, re.Match[str]] = {}
    for num, candidates in by_num.items():
        # Prefer candidates with "Tentative Ruling:"; among those, first by position.
        with_tr = [(m, ht) for m, ht in candidates if ht]
        if with_tr:
            best[num] = with_tr[0][0]
        else:
            best[num] = candidates[0][0]

    # Pass 3: build consecutive 1-based sequence from best picks,
    # verifying ascending positional order.
    selected: list[re.Match[str]] = []
    expected = 1
    last_pos = -1
    while expected in best:
        m = best[expected]
        if m.start() <= last_pos:
            # Position is not ascending — would cause overlapping text blocks.
            logger.warning(
                "Entry position not ascending; stopping split",
                expected=expected,
                position=m.start(),
                last_pos=last_pos,
            )
            break
        selected.append(m)
        last_pos = m.start()
        expected += 1

    return selected


def _split_rulings(text: str) -> list[SplitRuling]:
    """Split PDF text containing multiple numbered rulings into individual SplitRuling objects.

    Riverside PDFs use numbered entries like:
        1.
        CVPS2306157 YELDELL vs HENSS  Hearing re: Demurrer ...
        Tentative Ruling: ...

        2.
        CVPS2306202 CRUMP vs IRWIN  ...
        Tentative Ruling: ...

    Returns an empty list if no numbered entries are found.
    Returns a single-element list if only one entry exists.

    .. note:: Numbered points inside ruling body text (e.g.
       "1.\\nThe motion is granted") are filtered out by
       ``_filter_entry_matches`` to prevent off-by-one mis-assignment
       of ruling text to cases (#1410).
    """
    # Find all numbered entry positions
    all_matches = list(_RULING_ENTRY_RE.finditer(text))
    if not all_matches:
        return []

    # Filter to only real entry markers (skip numbered points in body text)
    matches = _filter_entry_matches(all_matches, text)
    if not matches:
        return []

    rulings: list[SplitRuling] = []
    for i, match in enumerate(matches):
        entry_num = int(match.group("num"))
        start = match.end()
        # End is either the start of the next entry or the end of text.
        # We look for the next entry's match start, minus any leading whitespace.
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(text)

        ruling_text = text[start:end].strip()

        # Remove "Page N of M" footers
        ruling_text = re.sub(r"\nPage \d+ of \d+\s*$", "", ruling_text).strip()

        # Extract case number
        case_match = _CASE_NUMBER_RE.search(ruling_text)
        case_number = case_match.group(0) if case_match else None

        # Extract case title (party vs party)
        case_title = _extract_case_title_from_ruling(ruling_text)

        # Extract motion type
        motion_type = _extract_motion_type(ruling_text)

        # Extract outcome
        outcome = _extract_outcome(ruling_text)

        rulings.append(
            SplitRuling(
                ruling_index=entry_num,
                case_number=case_number,
                ruling_text=ruling_text,
                case_title=case_title,
                motion_type=motion_type,
                outcome=outcome,
            )
        )

    return rulings


def _extract_case_title_from_ruling(text: str) -> str | None:
    """Extract case title in 'Plaintiff v. Defendant' format from ruling text.

    Riverside PDFs have the party names on the same line as the case number:

        CVPS2306157 YELDELL vs HENSS  <motion description>

    Or the case number is on a separate line but near a "vs" pattern:

        CVPS2404518 NIETO vs CREATING A   <rest is noise from columns>

    We find the line containing the case number, then look for "X vs Y"
    on that same line. The party names are typically ALL-CAPS single words
    or short phrases.
    """
    # Find the line containing the case number
    case_match = _CASE_NUMBER_RE.search(text[:500])
    if not case_match:
        return None

    # Get the line containing the case number
    line_start = text.rfind("\n", 0, case_match.start()) + 1
    line_end = text.find("\n", case_match.end())
    if line_end == -1:
        line_end = len(text)
    case_line = text[line_start:line_end].strip()

    # Look for "X vs Y" on the case line
    # Pattern: after case number, "PLAINTIFF vs DEFENDANT <rest>"
    # Use _CASE_NUMBER_RE.pattern to stay in sync with the main regex.
    _vs_pat = (
        _CASE_NUMBER_RE.pattern
        + r"\s+(?P<plaintiff>[A-Z][A-Z\s,.'-]+?)"
        + r"\s+vs\s+(?P<defendant>[A-Z][A-Z\s,.'-]+)"
    )
    vs_match = re.search(_vs_pat, case_line, re.IGNORECASE)
    if not vs_match:
        return None

    plaintiff = " ".join(vs_match.group("plaintiff").split()).strip()
    defendant = " ".join(vs_match.group("defendant").split()).strip()

    # Truncate defendant at motion-related keywords (common in these PDFs)
    for keyword in (
        "Hearing",
        "Motion",
        "Demurrer",
        "Complaint",
        "Sanctions",
        "Requests",
        "Request",
        "Order",
        "Application",
    ):
        # Case-insensitive truncation at word boundaries
        pattern = re.compile(r"\b" + keyword + r"\b", re.IGNORECASE)
        kw_match = pattern.search(defendant)
        if kw_match and kw_match.start() > 0:
            defendant = defendant[: kw_match.start()].strip()

    # Strip trailing punctuation and noise
    plaintiff = plaintiff.strip(" ,;:")
    defendant = defendant.strip(" ,;:")

    # Remove case number fragments that may appear in defendant
    defendant = _CASE_NUMBER_RE.sub("", defendant).strip()

    if not plaintiff or not defendant or len(plaintiff) < 2 or len(defendant) < 2:
        return None

    return f"{plaintiff.title()} v. {defendant.title()}"


def _extract_motion_type(text: str) -> str | None:
    """Extract motion type from the header area of the ruling text.

    The motion type appears in the lines before "Tentative Ruling:".
    Common patterns:
    - "Hearing re: Demurrer on 1st Amended Complaint..."
    - "Motion to Compel Plaintiff's Responses..."
    - "Motion for Judgment on the Pleadings..."
    - "MOTION TO DEEM REQUESTS FOR ADMISSIONS ADMITTED"
    """
    # Extract text before "Tentative Ruling:" — the motion description lives there
    tr_idx = text.lower().find("tentative ruling:")
    if tr_idx == -1:
        header = text[:500]
    else:
        header = text[:tr_idx]

    # Try specific patterns in order of reliability

    # Pattern 1: "Hearing re: <type>"
    m = re.search(r"Hearing re:\s*(?P<mt>.+?)(?:\s+on\s|\s+of\s|\s*$)", header, re.IGNORECASE)
    if m:
        return " ".join(m.group("mt").split()).strip().rstrip(" ,;:")

    # Pattern 2: "Motion to/for <type>"
    m = re.search(
        r"(?:MOTION|Motion)\s+(?:to|for)\s+(?P<mt>.+)",
        header,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        raw = " ".join(m.group("mt").split()).strip()
        # Truncate at party names / case numbers / common noise
        for truncate_pattern in [
            _CASE_NUMBER_RE,
            re.compile(r"\bby\s+[A-Z]", re.IGNORECASE),
            re.compile(r"\bvs\b", re.IGNORECASE),
        ]:
            trunc_match = truncate_pattern.search(raw)
            if trunc_match and trunc_match.start() > 3:
                raw = raw[: trunc_match.start()].strip()
        raw = raw.rstrip(" ,;:(")
        if len(raw) > 80:
            raw = raw[:80]
        return raw or None

    return None


def _extract_outcome(text: str) -> str | None:
    """Extract outcome from the ruling text.

    Looks for the final disposition, which in Riverside PDFs appears as
    one of:
    - "Tentative Ruling: Granted." / "Tentative Ruling: Denied."
    - "Demurrer is OVERRULED" / "Motion ... DENIED"
    - Final line like "Motion for ... DENIED"

    For rulings with long narrative text after "Tentative Ruling:", we scan
    the full text for disposition keywords instead of relying on just the
    first line.
    """
    text_lower = text.lower()

    # Check for explicit outcome keywords anywhere in the text, prioritising
    # patterns that appear near the end of the ruling (the disposition).
    # Search backwards through the text for the most specific match.

    # Pattern: "is OVERRULED/SUSTAINED/GRANTED/DENIED" (common at ruling end)
    disposition_re = re.compile(
        r"\b(?:is\s+)?(?P<outcome>overruled|sustained|granted|denied|moot)\b",
        re.IGNORECASE,
    )
    # Find the LAST occurrence (most likely to be the final disposition)
    matches = list(disposition_re.finditer(text))
    if matches:
        return matches[-1].group("outcome").capitalize()

    # "continued to ..." pattern
    if re.search(r"\bcontinued\s+to\b", text_lower):
        return "Continued"

    # "No tentative ruling" pattern
    m = _OUTCOME_RE.search(text)
    if m:
        raw = " ".join(m.group("outcome").split()).strip()
        if "no tentative" in raw.lower():
            return "No Tentative Ruling"
        if "hearing will be conducted" in raw.lower():
            return "No Tentative Ruling"

    return None


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

    Overrides fetch_documents to split multi-ruling PDFs into individual
    CapturedDocument records, each with its own case number, ruling text,
    parties, motion type, and outcome.
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
        """Fetch PDFs then split multi-ruling PDFs into individual documents.

        Boilerplate "No Tentative Rulings" PDFs are already filtered by the
        base-class ``_is_boilerplate()`` hook before reaching this method.
        """
        raw_docs = super().fetch_documents()
        split_docs: list[CapturedDocument] = []

        for doc in raw_docs:
            try:
                text = _extract_pdf_text(doc.raw_content)
            except Exception as exc:
                logger.warning("PDF text extraction failed", error=str(exc))
                split_docs.append(doc)
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

            rulings = _split_rulings(text)
            if len(rulings) <= 1:
                # Single ruling or no rulings — keep original doc
                split_docs.append(doc)
                continue

            # Extract hearing date from the full PDF text (it's in the header)
            hearing_date = _riv_hearing_date_from_text(text)

            logger.info(
                "Splitting multi-ruling PDF",
                department=doc.department,
                ruling_count=len(rulings),
            )
            for ruling in rulings:
                child = self._make_base_doc(
                    source_url=doc.source_url,
                    raw_content=doc.raw_content,
                    content_format=ContentFormat.PDF,
                )
                # Preserve parent metadata
                child.judge_name = doc.judge_name
                child.department = doc.department
                child.courthouse = doc.courthouse
                child.hearing_date = hearing_date
                child.extra = {**doc.extra}
                # Set per-ruling fields
                child.case_number = ruling.case_number
                child.ruling_text = ruling.ruling_text
                child.case_title = ruling.case_title
                child.motion_type = ruling.motion_type
                child.outcome = ruling.outcome
                child.extra["ruling_index"] = ruling.ruling_index
                child.extra["pre_split"] = True
                split_docs.append(child)

        return split_docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract fields from PDF text.

        If the document was pre-split by fetch_documents, skip the parent's
        parse_document (which would re-extract the full PDF text and overwrite
        our per-ruling fields) and only add the hearing date.
        """
        if doc.extra.get("pre_split"):
            # Already split — just extract hearing date from ruling text
            if doc.ruling_text and not doc.hearing_date:
                doc.hearing_date = _riv_hearing_date_from_text(doc.ruling_text)
            # Fallback: department-to-judge mapping for pre-split docs (#585)
            if not doc.judge_name and doc.department and self._dept_judge_map:
                from courts.ca.riverside_dept_judges import lookup_judge_for_department

                mapped_name = lookup_judge_for_department(self._dept_judge_map, doc.department)
                if mapped_name:
                    doc.judge_name = mapped_name
            return doc

        # Single-ruling PDF: use parent parse logic
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
