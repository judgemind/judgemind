"""Contra Costa County Superior Court — Tentative Rulings Scraper (Pattern 2 variant).

Verified against live site 2026-03-11:
  URL:  https://retired.cc-courts.org/civil/motions-hearings-tentative.aspx
  404 PDF links found on index page (across ~11 active departments)
  No Playwright required; httpx fetch works directly.

HTML structure:
  Each department is in a <div class='tr-dir'> container with a <span> header:
    "Department NN - Judge Name"
  PDF links use class='tentative-ruling' with href paths containing backslashes:
    href='TR\\Department NN - Judge Name\\NN_MMDDYY.pdf'
  Link text is just a date (e.g. "Mar 11, 2026"), not the department/judge info.

Department/judge info is extracted from the URL path, not from the link text.
This differs from other PdfLinkScraper courts where the link text carries the
department and judge name.

PDF filename pattern: NN_MMDDYY.pdf
  NN     = department number (zero-padded, e.g. 09, 16)
  MMDDYY = month + day + 2-digit year

PDF structure (civil departments):
  SUPERIOR COURT OF CALIFORNIA, CONTRA COSTA COUNTY
  MARTINEZ, CA
  DEPARTMENT 16
  JUDICIAL OFFICER: BENJAMIN T REYES II
  HEARING DATE: 03/11/2026
  [numbered case entries with CASE NUMBER, CASE NAME, hearing type, ruling]

PDF structure (probate departments 30, 38):
  SUPERIOR COURT OF CALIFORNIA, CONTRA COSTA COUNTY
  PR - MARTINEZ-WAKEFIELD TAYLOR COURTHOUSE
  COURT CALENDAR FOR MARCH 16, 2026
  DEPARTMENT 30
  JUDICIAL OFFICER: VIRGINIA M GEORGE
  [entries like: N25-2307 IN THE MATTER OF: AJAY BHALLA]

Case number formats:
  Civil:    C + 2-digit year + hyphen + 5 digits  (C24-02490)
  Limited:  L + 2-digit year + hyphen + 5 digits  (L23-06679)
  Probate:  N or P + 2-digit year + hyphen + 4-5 digits (N25-2307)

Investigation: #155
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx
import structlog
from bs4 import BeautifulSoup

from framework import CapturedDocument, ContentFormat, ScheduleWindow, ScraperConfig
from framework.extraction_config import get_county_extraction_config
from framework.llm_utils import parse_llm_json

from .pdf_link_scraper import PdfLinkScraper, _extract_pdf_text

logger = structlog.get_logger(__name__)

INDEX_URL = "https://retired.cc-courts.org/civil/motions-hearings-tentative.aspx"
BASE_URL = "https://retired.cc-courts.org/civil/"

# Case numbers: C24-02490, L23-06679, N25-2307, P24-1234
_CASE_NUMBER_RE = re.compile(r"\b[CLNP]\d{2}-\d{4,5}\b")

# Department/judge from URL path: "Department 16 - Judge Reyes"
# Handles judge names with extra info like "George (Probate)" or "Leonard Marquez"
_URL_DEPT_JUDGE_RE = re.compile(
    r"Department\s+(?P<department>\d+)\s*-\s*Judge\s+(?P<judge_name>[^/\\]+)",
    re.IGNORECASE,
)

# Judge name from PDF header: "JUDICIAL OFFICER: BENJAMIN T REYES II"
_JUDICIAL_OFFICER_RE = re.compile(
    r"JUDICIAL OFFICER:\s*(?P<judge_name>[^\n]+)",
    re.IGNORECASE,
)

# Hearing date from PDF header: "HEARING DATE: 03/11/2026"
_HEARING_DATE_PDF_RE = re.compile(
    r"HEARING DATE:\s*(?P<date>\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)

# Hearing date from probate header: "COURT CALENDAR FOR MARCH 16, 2026"
_HEARING_DATE_PROBATE_RE = re.compile(
    r"COURT CALENDAR FOR\s+(?P<date>(?:January|February|March|April|May|June"
    r"|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

# Filename date: NN_MMDDYY.pdf -> extract MMDDYY
_FILENAME_DATE_RE = re.compile(r"\d{2}_(\d{6})\.pdf$", re.IGNORECASE)

# Case name: "CASE NAME: SCOTT DINSDALE VS. CARDIGAN, INC."
_CASE_NAME_RE = re.compile(
    r"CASE NAME:\s*(?P<case_name>[^\n]+)",
    re.IGNORECASE,
)

# Probate case entry: "N25-2307 IN THE MATTER OF: AJAY BHALLA"
_PROBATE_ENTRY_RE = re.compile(
    r"(?P<case_number>[NP]\d{2}-\d{4,5})\s+IN THE MATTER OF:\s*(?P<case_name>[^\n]+)",
    re.IGNORECASE,
)

# Motion type from hearing line:
# "*HEARING ON MOTION IN RE: LEAVE TO FILE CROSS-COMPLAINT"
# "*CASE MANAGEMENT CONFERENCE"
# "HEARING ON PETITION FOR NAME CHANGE"
_MOTION_TYPE_RE = re.compile(
    r"\*?HEARING ON (?:MOTION IN RE:\s*)?(?P<motion_type>[^\n*]+)"
    r"|\*?(?P<motion_type2>CASE MANAGEMENT CONFERENCE"
    r"|FURTHER CASE MANAGEMENT CONFERENCE"
    r"|ORDER TO SHOW CAUSE"
    r"|STATUS CONFERENCE)",
    re.IGNORECASE,
)

# Outcome patterns from tentative ruling text.
# Values are lowercase DB ``ruling_outcome`` enum values so that
# ``_cc_extract_outcome()`` returns DB-ready strings without needing
# downstream ``normalize_outcome()`` (#2170).
_OUTCOME_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bis\s+granted\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bis\s+denied\b", re.IGNORECASE), "denied"),
    (re.compile(r"\bis\s+sustained\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bis\s+overruled\b", re.IGNORECASE), "denied"),
    (re.compile(r"\bis\s+continued\b", re.IGNORECASE), "continued"),
    (re.compile(r"\bis\s+moot\b", re.IGNORECASE), "moot"),
    (re.compile(r"\bis\s+vacated\b", re.IGNORECASE), "off_calendar"),
    (re.compile(r"\bis\s+unopposed and is granted\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bgranted as set forth\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bdenied as set forth\b", re.IGNORECASE), "denied"),
    (re.compile(r"\bPetition Approved\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bPetition Denied\b", re.IGNORECASE), "denied"),
    (
        re.compile(r"\bno appearance\s+(?:is\s+)?required\b", re.IGNORECASE),
        "other",
    ),
]


# ---------------------------------------------------------------------------
# Phantom-ruling guard — calendar header extraction (#3798)
# ---------------------------------------------------------------------------

# Format A (civil): "8. 9:00 AM CASE NUMBER: L25-09151"
# Case-insensitive, multiline.
_CALENDAR_HEADER_CIVIL_RE = re.compile(
    r"^\s*\d+[A-Z]?\.\s+\d{1,2}:\d{2}\s+(?:AM|PM)\s+CASE\s+NUMBER:\s+(\S+)",
    re.IGNORECASE | re.MULTILINE,
)

# Format B (probate): "7. P24-00230 CONSERVATORSHIP OF: JUNE STONE"
# Also matches: "1. N25-2307 IN THE MATTER OF: AJAY BHALLA"
# and: "5. MSP19-01440 CONS. OF MARIE ROST"
# Case-insensitive, multiline.
_CALENDAR_HEADER_PROBATE_RE = re.compile(
    r"^\s*\d+[A-Z]?\.\s+(?P<cn>(?:MSP|MS|RS|A)?[CLNP]?\d{2}-?\d{4,8}|MS\d{4})\s+"
    r"(?:IN\s+THE\s+MATTER\s+OF|CONS\.|CONSERVATORSHIP\s+OF)",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_calendar_header_case_numbers(text: str) -> set[str]:
    """Extract case numbers that appear in top-level numbered calendar headers.

    Scans the PDF text for Format A (civil) and Format B (probate) calendar
    header lines and returns the set of normalised (upper-stripped) case
    numbers found.  Only case numbers in actual calendar header lines are
    returned — body text is not scanned.

    Used by the post-LLM phantom-ruling guard in ``fetch_documents`` to
    validate that each LLM-extracted ruling corresponds to a real calendar
    entry in the source PDF (#3798).

    Args:
        text: Full PDF text as extracted by pdfplumber.

    Returns:
        Set of normalised case numbers (e.g. ``{'L25-09151', 'C24-02490'}``).
        Returns an empty set if no calendar header lines are found.
    """
    result: set[str] = set()

    for m in _CALENDAR_HEADER_CIVIL_RE.finditer(text):
        cn = m.group(1).upper().strip()
        if cn:
            result.add(cn)

    for m in _CALENDAR_HEADER_PROBATE_RE.finditer(text):
        cn = m.group("cn").upper().strip()
        if cn:
            result.add(cn)

    return result


# ---------------------------------------------------------------------------
# LLM extraction feature flag and configuration (#2053)
# ---------------------------------------------------------------------------


def _cc_llm_enabled() -> bool:
    """Return True when LLM-based extraction is enabled for CC rulings."""
    return os.environ.get("ENABLE_CC_LLM_EXTRACTION", "").lower() in (
        "1",
        "true",
        "yes",
    )


# Map LLM outcome values to lowercase enum values matching the DB schema.
_CC_OUTCOME_MAP: dict[str | None, str | None] = {
    "granted": "granted",
    "denied": "denied",
    "granted_in_part": "granted_in_part",
    "denied_in_part": "denied_in_part",
    "moot": "moot",
    "continued": "continued",
    "off_calendar": "off_calendar",
    "submitted": "submitted",
    "other": "other",
    None: None,
}


@dataclass
class CCSplitRuling:
    """A single ruling extracted from a multi-case CC department PDF via LLM."""

    ruling_index: int
    case_number: str | None
    ruling_text: str
    case_title: str | None = None
    case_type: str | None = None
    motion_type: str | None = None
    outcome: str | None = None
    parties: list[dict[str, str]] = dataclass_field(default_factory=list)


def _llm_extract_rulings(pdf_text: str) -> list[CCSplitRuling] | None:
    """Extract rulings from CC department PDF text using an LLM.

    Sends pdfplumber-extracted text to the LLM with the Contra Costa system
    prompt from ``framework.extraction_config``, and parses the JSON response
    into ``CCSplitRuling`` objects.

    Returns a list of ``CCSplitRuling`` objects on success, or ``None``
    if the LLM call fails or the response cannot be parsed.  The caller
    should fall back to the single-doc regex path when ``None`` is returned.

    The name is deliberately ``_llm_extract_rulings`` (no ``_cc_`` prefix)
    so that ``scripts/reingest_from_s3.py`` discovers it via the
    ``_LLM_SPLIT_REGISTRY`` attribute-name scan (#2469).
    """
    from ingestion.llm_providers import call_llm

    if not pdf_text or not pdf_text.strip():
        return None

    cc_config = get_county_extraction_config("CA", "Contra Costa")
    assert cc_config is not None, "Contra Costa must be registered in _COUNTY_CONFIGS"

    response = call_llm(
        system_prompt=cc_config.system_prompt,
        user_message=pdf_text,
        provider=cc_config.provider,
        model=cc_config.model,
        max_tokens=cc_config.max_output_tokens or 32768,
        timeout=60.0,
    )

    if response is None:
        logger.warning("cc.llm_extraction_failed", reason="null_response")
        return None

    try:
        data = parse_llm_json(response.text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("cc.llm_parse_failed", error=str(exc))
        return None

    # Accept both {"rulings": [...]} and bare [...] responses.
    if isinstance(data, list):
        rulings_list = data
    elif isinstance(data, dict):
        rulings_list = data.get("rulings", [])
    else:
        logger.warning("cc.llm_unexpected_shape", shape=type(data).__name__)
        return None

    if not isinstance(rulings_list, list):
        logger.warning("cc.llm_rulings_not_list")
        return None

    rulings: list[CCSplitRuling] = []
    for idx, entry in enumerate(rulings_list):
        if not isinstance(entry, dict):
            continue
        raw_outcome = entry.get("outcome")
        outcome = _CC_OUTCOME_MAP.get(raw_outcome)

        # Parse parties list
        raw_parties = entry.get("extracted_parties", [])
        parties: list[dict[str, str]] = []
        if isinstance(raw_parties, list):
            for p in raw_parties:
                if isinstance(p, dict) and p.get("name") and p.get("role"):
                    name = str(p["name"]).strip()
                    if len(name) > 200 or "\n" in name or "\r" in name:
                        continue
                    parties.append({"name": name, "role": str(p["role"])})

        rulings.append(
            CCSplitRuling(
                ruling_index=idx + 1,
                case_number=entry.get("extracted_case_number"),
                ruling_text=entry.get("ruling_text") or "",
                case_title=entry.get("extracted_case_title"),
                case_type=entry.get("case_type"),
                motion_type=entry.get("motion_type"),
                outcome=outcome,
                parties=parties,
            )
        )

    logger.info(
        "cc.llm_extraction_success",
        ruling_count=len(rulings),
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )

    return rulings


def _cc_hearing_date_from_filename(filename: str) -> datetime | None:
    """Parse hearing date from CC filename like '16_031126.pdf' -> 03/11/2026."""
    m = _FILENAME_DATE_RE.search(filename)
    if not m:
        return None
    mmddyy = m.group(1)
    try:
        month = int(mmddyy[0:2])
        day = int(mmddyy[2:4])
        year = 2000 + int(mmddyy[4:6])
        return datetime(year, month, day)
    except (ValueError, IndexError):
        return None


def _cc_hearing_date_from_pdf(text: str) -> datetime | None:
    """Extract hearing date from CC PDF text.

    Tries two formats:
    - Civil: "HEARING DATE: 03/11/2026"
    - Probate: "COURT CALENDAR FOR MARCH 16, 2026"
    """
    m = _HEARING_DATE_PDF_RE.search(text)
    if m:
        try:
            return datetime.strptime(m.group("date"), "%m/%d/%Y")
        except ValueError:
            pass

    m = _HEARING_DATE_PROBATE_RE.search(text)
    if m:
        raw = " ".join(m.group("date").split())
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue

    return None


def _cc_judge_from_pdf(text: str) -> str | None:
    """Extract judge name from CC PDF header (JUDICIAL OFFICER line)."""
    m = _JUDICIAL_OFFICER_RE.search(text)
    if m:
        raw = m.group("judge_name").strip()
        # Title-case if all upper
        if raw.isupper():
            return raw.title()
        return raw
    return None


def _cc_extract_outcome(text: str) -> str | None:
    """Extract outcome from a ruling section text.

    Searches for disposition keywords, preferring the last match
    (most likely to be the final disposition).
    """
    # Check specific patterns in order of specificity
    for pattern, outcome in _OUTCOME_PATTERNS:
        if pattern.search(text):
            return outcome
    return None


def _cc_extract_motion_type(text: str) -> str | None:
    """Extract motion type from hearing line in case entry."""
    m = _MOTION_TYPE_RE.search(text)
    if not m:
        return None
    raw = m.group("motion_type") or m.group("motion_type2")
    if raw:
        raw = raw.strip().rstrip("*")
        # Normalize whitespace
        raw = " ".join(raw.split())
        # Title-case if ALL CAPS
        if raw.isupper():
            raw = raw.title()
        # Truncate at common noise words
        for kw in ("Filed By", "Continued From"):
            idx = raw.upper().find(kw.upper())
            if idx > 0:
                raw = raw[:idx].strip()
        raw = raw.rstrip(" ,;:(")
        return raw if raw else None
    return None


def _cc_courthouse(dept: str) -> str | None:
    """Map department to courthouse.

    Most departments are in Martinez. Department 14 is in Richmond.
    """
    if dept == "14":
        return "Richmond Courthouse"
    return "Martinez Courthouse"


# ---------------------------------------------------------------------------
# Custom link extraction for CC's ASP.NET page
# ---------------------------------------------------------------------------


def _cc_extract_links(
    html: str,
    base_url: str,
) -> list[tuple[str, str, str | None, str | None]]:
    """Extract PDF links from Contra Costa's ASP.NET index page.

    Returns list of (absolute_pdf_url, link_text, department, judge_name).

    The CC page uses:
    - class='tentative-ruling' on <a> tags
    - Backslash paths in href: TR\\Department 16 - Judge Reyes\\16_031126.pdf
    - Department/judge info in the URL path, not in the link text
    - Link text is just a date like "Mar 11, 2026"
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str, str | None, str | None]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", class_="tentative-ruling"):
        href = a.get("href", "")
        if not href or ".pdf" not in href.lower():
            continue

        # Normalize Windows backslashes to forward slashes
        href_normalized = href.replace("\\", "/")

        # Skip temp files (like ~$_091525.pdf)
        if "~$" in href_normalized:
            continue

        # Build absolute URL
        abs_url = urljoin(base_url, href_normalized)

        if abs_url in seen:
            continue
        seen.add(abs_url)

        # Extract department and judge from URL path
        department: str | None = None
        judge_name: str | None = None
        m = _URL_DEPT_JUDGE_RE.search(href_normalized)
        if m:
            department = m.group("department")
            raw_judge = m.group("judge_name").strip()
            # Clean up: remove "(Probate)" suffix but preserve multi-word names
            judge_name = re.sub(r"\s*\(Probate\)\s*$", "", raw_judge, flags=re.IGNORECASE).strip()
            # Normalize whitespace
            judge_name = " ".join(judge_name.split()) if judge_name else None

        # Link text is typically a date like "Mar 11, 2026"
        link_text = a.get_text(separator=" ", strip=True).replace("\xa0", " ")

        results.append((abs_url, link_text, department, judge_name))

    return results


# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------


class CCTentativeRulingsScraper(PdfLinkScraper):
    """Contra Costa County tentative rulings — custom PDF-link variant.

    Overrides fetch_documents because CC's HTML structure differs from the
    standard PdfLinkScraper pattern:
    - Links use class='tentative-ruling' with backslash paths
    - Department/judge info is in the URL path, not in link text
    - Need custom link extraction logic

    Overrides parse_document to extract CC-specific fields from PDF text,
    including the structured case entry format.
    """

    def __init__(self, config: ScraperConfig, **kwargs: Any) -> None:
        # We pass a dummy PdfLinkConfig since we override fetch_documents.
        # The link_text_re is unused because we extract dept/judge from URL paths.
        from .pdf_link_scraper import PdfLinkConfig

        pdf_config = PdfLinkConfig(
            index_url=INDEX_URL,
            pdf_base_url=BASE_URL,
            link_text_re=re.compile(r"(?P<department>UNUSED)(?P<judge_name>UNUSED)"),
            courthouse_from_dept=_cc_courthouse,
            verify_ssl=True,
            filter_by_link_text=False,  # CC overrides fetch_documents entirely
            case_number_re=_CASE_NUMBER_RE,
        )
        super().__init__(config, pdf_config=pdf_config, **kwargs)

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch PDFs using custom link extraction for CC's ASP.NET page.

        When LLM extraction is enabled (``ENABLE_CC_LLM_EXTRACTION``), each
        PDF is split into individual rulings via LLM, producing one
        ``CapturedDocument`` per case entry.  If LLM extraction fails for a
        PDF, falls back to the single-doc-per-PDF regex path.
        """
        docs: list[CapturedDocument] = []

        use_llm = _cc_llm_enabled()
        if use_llm:
            self._log.info("CC LLM extraction enabled")

        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
            verify=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            self._log.info("Fetching index page", url=INDEX_URL)
            response = client.get(INDEX_URL)
            response.raise_for_status()

            links = _cc_extract_links(response.text, BASE_URL)
            self._log.info("Found PDF links", count=len(links))

            # Only fetch the most recent PDF per department (tentative rulings
            # are ephemeral — we want current rulings, not historical ones).
            # Group by department and take the first (most recent) for each.
            seen_depts: set[str | None] = set()
            recent_links: list[tuple[str, str, str | None, str | None]] = []
            for href, link_text, dept, judge in links:
                if dept not in seen_depts:
                    seen_depts.add(dept)
                    recent_links.append((href, link_text, dept, judge))

            self._log.info(
                "Fetching most recent PDF per department",
                department_count=len(recent_links),
            )

            for href, link_text, department, judge_name in recent_links:
                time.sleep(self.config.request_delay_seconds)
                try:
                    pdf_response = client.get(href)
                    pdf_response.raise_for_status()

                    pdf_content = pdf_response.content

                    # Extract hearing date from filename
                    filename = href.rsplit("/", 1)[-1] if "/" in href else href
                    hearing_date = _cc_hearing_date_from_filename(filename)

                    # Check for boilerplate
                    try:
                        text = _extract_pdf_text(pdf_content)
                    except Exception:
                        text = None

                    if text is not None and self._is_boilerplate(text):
                        self._log.info(
                            "Skipping boilerplate PDF",
                            department=department,
                            judge=judge_name,
                            url=href,
                        )
                        continue

                    # Fallback: recover hearing_date from PDF cover-page header
                    # when the filename-date regex misses (e.g. probate PDFs use
                    # content-hash filenames like 594361f8.pdf, not NN_MMDDYY.pdf).
                    if hearing_date is None and text is not None:
                        hearing_date = _cc_hearing_date_from_pdf(text)

                    # LLM extraction path: send PDF text to LLM, get back
                    # structured rulings with all fields.
                    if use_llm and text is not None:
                        # Refine judge name from PDF header for authoritative
                        # metadata passed to LLM-extracted docs.
                        pdf_judge = _cc_judge_from_pdf(text)
                        effective_judge = pdf_judge or judge_name

                        llm_rulings = _llm_extract_rulings(text)
                        if llm_rulings is not None:
                            # Hoist valid_cns computation once per PDF (#3798).
                            # Used by the phantom-ruling guard below.
                            valid_cns = _extract_calendar_header_case_numbers(text)
                            for ruling in llm_rulings:
                                # Phantom-ruling guard (#3798): drop LLM rulings
                                # whose case_number does not appear in a top-level
                                # calendar header in the source PDF.  Rulings with
                                # case_number=None pass through (caught downstream).
                                if ruling.case_number is not None:
                                    cn_norm = ruling.case_number.upper().strip()
                                    if valid_cns and cn_norm not in valid_cns:
                                        self._log.warning(
                                            "cc.phantom_ruling_dropped",
                                            dropped_case_number=ruling.case_number,
                                            legitimate_count=len(valid_cns),
                                            department=department,
                                            judge=effective_judge,
                                            source_url=href,
                                        )
                                        continue
                                doc = self._make_base_doc(
                                    source_url=href,
                                    raw_content=pdf_content,
                                    content_format=ContentFormat.PDF,
                                )
                                doc.department = department
                                doc.judge_name = effective_judge
                                if department:
                                    doc.courthouse = _cc_courthouse(department)
                                doc.extra["link_text"] = link_text
                                if hearing_date:
                                    doc.hearing_date = hearing_date
                                # Pre-populate fields from LLM extraction
                                doc.case_number = ruling.case_number
                                doc.case_title = ruling.case_title
                                doc.ruling_text = ruling.ruling_text
                                doc.motion_type = ruling.motion_type
                                doc.outcome = ruling.outcome
                                # Regex fallback for null LLM fields (#4029)
                                if doc.outcome is None and doc.ruling_text:
                                    doc.outcome = _cc_extract_outcome(doc.ruling_text)
                                if doc.motion_type is None and doc.ruling_text:
                                    doc.motion_type = _cc_extract_motion_type(doc.ruling_text)
                                doc.parties = ruling.parties
                                doc.extra["_llm_extracted"] = True
                                doc.extra["pre_split"] = True
                                doc.extra["ruling_index"] = ruling.ruling_index
                                if ruling.case_type:
                                    doc.extra["case_type"] = ruling.case_type
                                docs.append(doc)
                            self._log.debug(
                                "LLM extracted rulings",
                                dept=department,
                                judge=effective_judge,
                                cases=len(llm_rulings),
                            )
                            continue
                        # LLM failed — fall through to single-doc regex path
                        self._log.warning(
                            "LLM extraction failed, falling back to regex",
                            department=department,
                            judge=judge_name,
                        )

                    # Single-doc regex path (default, or LLM fallback)
                    doc = self._make_base_doc(
                        source_url=href,
                        raw_content=pdf_content,
                        content_format=ContentFormat.PDF,
                    )
                    doc.department = department
                    doc.judge_name = judge_name
                    if department:
                        doc.courthouse = _cc_courthouse(department)
                    doc.extra["link_text"] = link_text
                    if hearing_date:
                        doc.hearing_date = hearing_date

                    docs.append(doc)
                    self._log.debug(
                        "Fetched PDF",
                        judge=judge_name,
                        dept=department,
                        url=href,
                    )
                except Exception as exc:
                    self._log.error(
                        "Failed to fetch PDF",
                        url=href,
                        department=department,
                        error=str(exc),
                    )

        return docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract structured fields from CC PDF text.

        If the document was pre-split by ``fetch_documents`` (via the LLM
        extraction path), skip regex extraction — the per-ruling fields
        (``case_number``, ``case_title``, ``ruling_text``, ``motion_type``,
        ``outcome``, ``parties``) have already been populated from the LLM
        response, and re-running regex over the full PDF text would
        overwrite them with whatever matches first in the multi-case PDF.

        This mirrors the Fresno and LA guards (#2469).
        """
        if doc.extra.get("pre_split") or doc.extra.get("_llm_extracted"):
            return doc

        try:
            text = _extract_pdf_text(doc.raw_content)
            doc.ruling_text = text
        except Exception as exc:
            self._log.warning("PDF text extraction failed", error=str(exc))
            return doc

        # Extract/refine judge name from PDF header
        if not doc.judge_name:
            doc.judge_name = _cc_judge_from_pdf(text)
        elif doc.judge_name and doc.ruling_text:
            # PDF header has the full name — prefer it over the URL-path name
            pdf_judge = _cc_judge_from_pdf(text)
            if pdf_judge:
                doc.judge_name = pdf_judge

        # Extract hearing date from PDF text if not already set from filename
        if not doc.hearing_date:
            doc.hearing_date = _cc_hearing_date_from_pdf(text)

        # Extract case numbers
        case_numbers = _CASE_NUMBER_RE.findall(text)
        if case_numbers:
            doc.case_number = case_numbers[0]
            if len(case_numbers) > 1:
                doc.extra["all_case_numbers"] = list(dict.fromkeys(case_numbers))

        # Extract case title from first case entry
        case_name_match = _CASE_NAME_RE.search(text)
        if case_name_match:
            raw_title = case_name_match.group("case_name").strip()
            # Title-case if all uppercase
            if raw_title.isupper():
                raw_title = raw_title.title()
            doc.case_title = raw_title
        else:
            # Try probate pattern
            probate_match = _PROBATE_ENTRY_RE.search(text)
            if probate_match:
                raw_title = probate_match.group("case_name").strip()
                if raw_title.isupper():
                    raw_title = raw_title.title()
                doc.case_title = raw_title

        # Extract motion type from first hearing line
        doc.motion_type = _cc_extract_motion_type(text)

        # Extract outcome from ruling text
        doc.outcome = _cc_extract_outcome(text)

        return doc


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default Contra Costa scraper configuration."""
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-cc-tentatives",
        state="CA",
        county="Contra Costa",
        court="Superior Court",
        target_urls=[INDEX_URL],
        poll_interval_seconds=43200,  # twice daily
        schedule_windows=[
            # Civil rulings posted by 1:30 PM day before hearing
            ScheduleWindow(start=dtime(14, 0), end=dtime(15, 0)),  # 2 PM sweep
            ScheduleWindow(start=dtime(21, 0), end=dtime(22, 0)),  # 9 PM catch-up
        ],
        request_delay_seconds=1.0,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
