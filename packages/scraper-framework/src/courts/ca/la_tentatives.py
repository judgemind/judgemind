"""LA County Superior Court — Civil Tentative Rulings Scraper (Pattern 1).

Strategy: enumerate all (courthouse, department, date) combinations from the
ASP.NET dropdown, POST for each, archive the raw HTML per-department response.

Verified against live site 2026-03-02:
- URL: https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil
- Select name: ctl00$ctl00$siteMasterHolder$basicBodyHolder$List2DeptDate
- Select id:   siteMasterHolder_basicBodyHolder_List2DeptDate
- Option value format: "ALH,3,03/02/2026"  (courthouse_code,dept,MM/DD/YYYY)
- Option text format:  "(Alhambra Courthouse:  Dept. 3) March 2, 2026"
- Ruling content:      div#speechSynthesis
- Multiple cases may appear in a single department response
- Judge name in: <div>...Name Judge of the Superior Court</div>
- No CAPTCHA on civil tentatives; simple HTTP sufficient (no Playwright needed)
- Recommended schedule: 6 PM primary, 2 AM catch-up
"""

from __future__ import annotations

import functools
import json
import os
import re
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime

import httpx
import structlog
from bs4 import BeautifulSoup

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig
from framework.court_directory import CourtDirectory
from framework.events import EventBus
from framework.la_parser_utils import (
    BARE_ROLE_LABELS as _BARE_ROLE_LABELS,
)
from framework.la_parser_utils import (
    CASE_NAME_FIELD_RE as _CASE_NAME_FIELD_RE,
)
from framework.la_parser_utils import (
    CASE_PARTIES_RE as _CASE_PARTIES_RE,
)
from framework.la_parser_utils import (
    ENTITY_DESCRIPTOR_RE as _ENTITY_DESCRIPTOR_RE,
)
from framework.la_parser_utils import (
    MOVING_PARTY_RE as _MOVING_PARTY_RE,
)
from framework.la_parser_utils import (
    RESPONDING_PARTY_RE as _RESPONDING_PARTY_RE,
)
from framework.la_parser_utils import (
    ROLE_MAP as _ROLE_MAP,
)
from framework.la_parser_utils import (
    ROLE_PREFIX_RE as _ROLE_PREFIX_RE,
)
from framework.la_parser_utils import (
    SKIP_RESPONDING_PHRASES as _SKIP_RESPONDING_PHRASES,
)
from framework.party_utils import (
    is_name_fragment as _is_name_fragment,  # noqa: F401 — re-exported for tests
)
from framework.party_utils import split_party_names as _split_party_names
from framework.storage import S3Archiver

logger = structlog.get_logger(__name__)

CIVIL_URL = "https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil"

# Verified field names from live site
SELECT_NAME = "ctl00$ctl00$siteMasterHolder$basicBodyHolder$List2DeptDate"
SELECT_ID = "siteMasterHolder_basicBodyHolder_List2DeptDate"

# Option value: "ALH,3,03/02/2026" — courthouse_code, dept, MM/DD/YYYY
_OPTION_VALUE_RE = re.compile(
    r"^(?P<courthouse_code>[^,]+),(?P<department>[^,]+),(?P<date>\d{2}/\d{2}/\d{4})$"
)

# Option text: "(Alhambra Courthouse:  Dept. 3) March 2, 2026"
_OPTION_TEXT_RE = re.compile(
    r"\((?P<courthouse>[^:]+):\s+Dept\.\s+(?P<dept>[^)]+)\)\s+(?P<date>.+)"
)

# Case numbers in ruling text: "Case Number:24NNCV02551" (no space)
_CASE_NUMBER_RE = re.compile(r"Case Number:\s*(\w+)")

# Split boundary: <HR> (optionally with attributes) followed by a Case Number header.
# Matches the boundary *between* cases inside the speechSynthesis div.
# Uses a lookahead so the Case Number header stays in the second segment.
_CASE_SPLIT_RE = re.compile(
    r"<HR[^>]*>\s*(?:<P>)?\s*(?=<B>\s*Case Number:\s*</B>)",
    re.IGNORECASE,
)

# Lightweight HTML tag stripper used in _split_cases_html instead of a full
# BeautifulSoup parse.  Only needs to be accurate enough for a "Case Number:"
# substring check, not for rendering.
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Judge name: "<div>William A. Crowfoot Judge of the Superior Court</div>"
_JUDGE_DIV_RE = re.compile(r"(.+?)\s+Judge of the Superior Court", re.DOTALL)

# Case title extraction from party caption block.
# The party section text typically looks like:
#   "SUMAYYA AASI, et al.,\n  Plaintiff(s),\n  vs.\n  AMERICAN HONDA...,\n  Defendant(s)."
# We capture the plaintiff/petitioner name and defendant/respondent name around "vs."
_CASE_TITLE_RE = re.compile(
    r"^(?P<plaintiff>.+?),?\s*\n\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?,?"
    r"\s+vs\.\s+"
    r"(?P<defendant>.+?),?\s*\n\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?\.?",
    re.DOTALL | re.MULTILINE,
)

# _CASE_PARTIES_RE, _ROLE_MAP, _MOVING_PARTY_RE, _RESPONDING_PARTY_RE,
# _ROLE_PREFIX_RE, _BARE_ROLE_LABELS, _CASE_NAME_FIELD_RE are imported from
# framework.la_parser_utils above.  See #1465 for deduplication rationale.

# Marker present in the LA Court stale-ViewState error page (HTTP 200, ~8KB).
# When the POST uses an expired ViewState or a dropdown option that no longer
# exists, the server returns this error page instead of ruling content.
_LA_ERROR_MARKER = "We're sorry"

# Department header boilerplate pattern.  Pages like "DEPARTMENT 15 LAW AND
# MOTION RULINGS" contain no actual ruling content and should be skipped.
_DEPT_HEADER_BOILERPLATE_RE = re.compile(
    r"DEPARTMENT\s+\S+\s+LAW AND MOTION RULINGS",
    re.IGNORECASE,
)

# _ENTITY_DESCRIPTOR_RE is imported from framework.la_parser_utils above.

# Maximum length for a cleaned case title.  Titles longer than this after
# entity-descriptor stripping still contain too much caption noise.
_MAX_TITLE_LENGTH = 120


# ---------------------------------------------------------------------------
# LLM extraction feature flag and configuration (#1938)
# ---------------------------------------------------------------------------


def _la_llm_enabled() -> bool:
    """Return True when LLM-based extraction is enabled for LA rulings."""
    return os.environ.get("ENABLE_LA_LLM_EXTRACTION", "").lower() in (
        "1",
        "true",
        "yes",
    )


# Default LLM provider and model for LA text extraction.
_LA_LLM_PROVIDER = "google"
_LA_LLM_MODEL = "gemini-2.5-flash-lite"

# Map LLM outcome values to lowercase enum values matching the DB schema.
_OUTCOME_MAP: dict[str | None, str | None] = {
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
class LASplitRuling:
    """A single ruling extracted from a multi-case LA department page via LLM."""

    ruling_index: int
    case_number: str | None
    ruling_text: str
    case_title: str | None = None
    motion_type: str | None = None
    outcome: str | None = None
    parties: list[dict[str, str]] = dataclass_field(default_factory=list)


LA_SYSTEM_PROMPT = (
    "You are a legal document parser for California court "
    "tentative rulings from Los Angeles County Superior Court.\n\n"
    "You will receive the text content extracted from an HTML page "
    "containing tentative rulings for one department.  Your job is to "
    "identify EVERY individual case ruling in the document and extract "
    "structured data for each.\n\n"
    "## LA County Document Format\n\n"
    "LA County tentative ruling pages have this structure:\n"
    "1. **Department header**: 'DEPARTMENT {N} LAW AND MOTION RULINGS' "
    "followed by the hearing date.\n"
    "2. **Case entries**: Each case starts with 'Case Number: {NUMBER}' "
    "followed by:\n"
    "   - Party caption block (Plaintiff vs. Defendant with roles)\n"
    "   - Motion description\n"
    "   - The ruling text\n"
    "   - Judge signature: '{Name} Judge of the Superior Court'\n"
    "3. **Multiple cases**: Department pages may contain rulings for "
    "multiple cases separated by horizontal rules.\n"
    "4. **Party caption format**: Parties are listed in a formal block:\n"
    "   'PLAINTIFF NAME, et al.,\\n  Plaintiff(s),\\n  vs.\\n  "
    "DEFENDANT NAME,\\n  Defendant(s).'\n"
    "5. **Moving/Responding parties**: Some rulings list "
    "'MOVING PARTY:' and 'RESPONDING PARTY:' fields.\n\n"
    "## Case Number Formats\n\n"
    "LA case numbers use these patterns:\n"
    "- Year + courthouse code + sequence: 24NNCV02551, 26NNCP00062, "
    "24STCV02143\n"
    "- Courthouse codes: NN=Northeast (Alhambra/Pasadena), "
    "ST=Stanley Mosk, BH=Beverly Hills, CH=Chatsworth, "
    "VE=Van Nuys, TO=Torrance, CO=Compton, EA=East LA\n"
    "- Case type codes: CV=Civil, CP=Complex/Probate, LC=Limited Civil\n\n"
    "## Rules\n\n"
    "1. Count and return EVERY case as a separate ruling. If the document "
    "has 3 'Case Number:' entries, return 3 rulings.\n"
    "2. Extract the case number EXACTLY as it appears (including any "
    "leading digits).\n"
    "3. For case_title, use 'Plaintiff v. Defendant' format. Strip "
    "legal entity descriptors like 'an individual', 'a corporation', "
    "etc. Use title case.\n"
    "4. For ruling_text, include the COMPLETE ruling text verbatim. "
    "Include all legal analysis, citations, and reasoning. Do NOT "
    "truncate or summarize.\n"
    "5. For parties, extract each party with their role (plaintiff, "
    "defendant, petitioner, respondent, cross-complainant, "
    "cross-defendant, moving_party, responding_party).\n"
    "6. Skip the department header boilerplate — only extract from "
    "case entries.\n"
    "7. Pages with only department headers and no 'Case Number:' "
    "entries have zero cases — return an empty rulings array.\n"
    "8. For pages that are error pages or contain 'We\\'re sorry', "
    "return an empty rulings array.\n\n"
    "## Outcome taxonomy\n\n"
    "Use EXACTLY one of these values:\n"
    "- granted — motion was fully granted\n"
    "- denied — motion was fully denied\n"
    "- granted_in_part — partially granted and partially denied\n"
    "- denied_in_part — partially denied\n"
    "- moot — motion is moot\n"
    "- continued — hearing was postponed\n"
    "- off_calendar — hearing removed from calendar\n"
    "- submitted — taken under submission\n"
    "- other — none of the above fit\n\n"
    "For 'overruled' (demurrers), map to 'denied'.\n"
    "For 'sustained' (demurrers), map to 'granted'.\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object, no other text:\n\n"
    "{\n"
    '  "extracted_judge_name": "First M. Last" or null,\n'
    '  "hearing_date": "YYYY-MM-DD" or null,\n'
    '  "department": "3" or null,\n'
    '  "rulings": [\n'
    "    {\n"
    '      "extracted_case_number": "24NNCV02551" or null,\n'
    '      "extracted_case_title": "Aasi v. American Honda Motor Co." '
    "or null,\n"
    '      "case_type": "civil" or null,\n'
    '      "outcome": "granted" or null,\n'
    '      "motion_type": "motion_to_compel" or null,\n'
    '      "ruling_text": "Full verbatim text..." or null,\n'
    '      "parties": [\n'
    '        {"name": "Sumayya Aasi", "role": "plaintiff"},\n'
    '        {"name": "American Honda Motor Co., Inc.", '
    '"role": "defendant"}\n'
    "      ]\n"
    "    }\n"
    "  ]\n"
    "}"
)


def _llm_extract_rulings(ruling_html: str) -> list[LASplitRuling] | None:
    """Extract rulings from LA department HTML using an LLM.

    Preprocesses the HTML to plain text, sends it to the LLM with a
    LA-specific system prompt, and parses the JSON response into
    ``LASplitRuling`` objects.

    Returns a list of ``LASplitRuling`` objects on success, or ``None``
    if the LLM call fails or the response cannot be parsed.  The caller
    should fall back to ``_split_cases_html()`` when ``None`` is returned.
    """
    from ingestion.llm_extract import preprocess_html
    from ingestion.llm_providers import call_llm

    text = preprocess_html(ruling_html)
    if not text or not text.strip():
        return None

    response = call_llm(
        system_prompt=LA_SYSTEM_PROMPT,
        user_message=text,
        provider=_LA_LLM_PROVIDER,
        model=_LA_LLM_MODEL,
        max_tokens=32768,
        timeout=60.0,
    )

    if response is None:
        logger.warning("la.llm_extraction_failed", reason="null_response")
        return None

    try:
        raw = response.text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [line for line in lines if not line.strip().startswith("```")]
            raw = "\n".join(lines)

        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("la.llm_parse_failed", error=str(exc))
        return None

    # Accept both {"rulings": [...]} and bare [...] responses.
    if isinstance(data, list):
        rulings_list = data
    elif isinstance(data, dict):
        rulings_list = data.get("rulings", [])
    else:
        logger.warning("la.llm_unexpected_shape", shape=type(data).__name__)
        return None

    if not isinstance(rulings_list, list):
        logger.warning("la.llm_rulings_not_list")
        return None

    rulings: list[LASplitRuling] = []
    for idx, entry in enumerate(rulings_list):
        if not isinstance(entry, dict):
            continue
        raw_outcome = entry.get("outcome")
        outcome = _OUTCOME_MAP.get(raw_outcome)

        # Parse parties list
        raw_parties = entry.get("parties", [])
        parties: list[dict[str, str]] = []
        if isinstance(raw_parties, list):
            for p in raw_parties:
                if isinstance(p, dict) and p.get("name") and p.get("role"):
                    name = str(p["name"]).strip()
                    if len(name) > 200 or "\n" in name or "\r" in name:
                        continue
                    parties.append({"name": name, "role": str(p["role"])})

        rulings.append(
            LASplitRuling(
                ruling_index=idx + 1,
                case_number=entry.get("extracted_case_number"),
                ruling_text=entry.get("ruling_text") or "",
                case_title=entry.get("extracted_case_title"),
                motion_type=entry.get("motion_type"),
                outcome=outcome,
                parties=parties,
            )
        )

    logger.info(
        "la.llm_extraction_success",
        ruling_count=len(rulings),
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )

    # Replace LLM-generated ruling_text with BeautifulSoup-extracted text
    # from per-case HTML chunks.  The LLM is asked to reproduce ruling text
    # verbatim, but for long rulings it hits the output token limit (~50k
    # chars), producing truncated text.  HTML-based extraction is lossless
    # and avoids token-limit truncation entirely (#2007).
    _replace_ruling_text_from_html(ruling_html, rulings)

    return rulings


def _replace_ruling_text_from_html(
    ruling_html: str,
    rulings: list[LASplitRuling],
) -> None:
    """Replace LLM-generated ruling_text with text extracted from HTML chunks.

    Uses ``_split_cases_html()`` to get per-case HTML sections, then
    ``BeautifulSoup.get_text()`` for lossless text extraction.  Matches
    chunks to LLM rulings by case number or by position (index).

    Modifies ``rulings`` in-place.  If chunking fails or produces no
    results, leaves the LLM-generated text unchanged.
    """
    case_htmls = _split_cases_html(ruling_html)
    if not case_htmls:
        return

    # Build a map from case number to HTML-extracted text.
    chunk_texts: list[tuple[str | None, str]] = []
    for html_chunk in case_htmls:
        soup = BeautifulSoup(html_chunk, "lxml")
        content = soup.find("div", id="speechSynthesis")
        if content:
            text = content.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)
        # Extract case number from this chunk for matching.
        case_num_match = _CASE_NUMBER_RE.search(text)
        case_num = case_num_match.group(1) if case_num_match else None
        chunk_texts.append((case_num, text))

    # Match each ruling to its HTML chunk.
    for ruling in rulings:
        matched_text: str | None = None

        if ruling.case_number:
            # Try case-number match.
            for chunk_case_num, chunk_text in chunk_texts:
                if chunk_case_num and chunk_case_num == ruling.case_number:
                    matched_text = chunk_text
                    break
            # If case_number was present but didn't match any chunk, do NOT
            # fall back to positional matching — that risks assigning text
            # from the wrong case.  Leave the LLM text unchanged.
            if matched_text is None:
                logger.warning(
                    "la_tentatives: LLM case number did not match any HTML chunk",
                    llm_case_number=ruling.case_number,
                    ruling_index=ruling.ruling_index,
                )
                continue
        else:
            # No case number from LLM — use positional match as last resort.
            idx = ruling.ruling_index - 1
            if 0 <= idx < len(chunk_texts):
                matched_text = chunk_texts[idx][1]

        if matched_text and len(matched_text) > 100:
            ruling.ruling_text = matched_text


def _split_rulings(text: str) -> list[LASplitRuling]:
    """Split an LA department page into per-case rulings using regex + HTML parsing.

    This is the non-LLM splitting path registered in ``_SPLIT_REGISTRY`` for
    use by ``reingest_from_s3._full_reparse_document()``.  It provides a
    reliable fallback when the LLM splitter is unavailable or fails.

    The ``text`` parameter is the raw HTML content (for HTML documents,
    ``_extract_text_from_content()`` returns the decoded HTML string).

    Uses ``_split_cases_html()`` for splitting and ``_extract_ruling_fields()``
    for per-case field extraction via BeautifulSoup.

    Returns an empty list if no case sections are found.
    """
    case_htmls = _split_cases_html(text)
    if not case_htmls:
        return []

    rulings: list[LASplitRuling] = []
    for idx, case_html in enumerate(case_htmls):
        # Create a minimal CapturedDocument for _extract_ruling_fields.
        soup = BeautifulSoup(case_html, "lxml")
        content = soup.find("div", id="speechSynthesis")
        if content:
            ruling_text = content.get_text(separator="\n", strip=True)
        else:
            ruling_text = soup.get_text(separator="\n", strip=True)

        if not ruling_text or len(ruling_text) < 50:
            continue

        # Extract case number.
        case_number: str | None = None
        case_numbers = _CASE_NUMBER_RE.findall(ruling_text)
        if case_numbers:
            case_number = case_numbers[0]

        # Extract case title.
        case_title = _extract_case_title(content if content else soup)

        # Extract parties for matching/display.
        parties: list[dict[str, str]] = []
        raw_parties = _extract_parties(content if content else soup)
        if raw_parties:
            for p in raw_parties:
                if isinstance(p, dict) and p.get("name") and p.get("role"):
                    parties.append({"name": str(p["name"]), "role": str(p["role"])})

        rulings.append(
            LASplitRuling(
                ruling_index=idx + 1,
                case_number=case_number,
                ruling_text=ruling_text,
                case_title=case_title,
                motion_type=None,  # Filled by enrichment pipeline.
                outcome=None,  # Filled by enrichment pipeline.
                parties=parties,
            )
        )

    return rulings


def _truncate_party_list(party_side: str) -> str:
    """Truncate a multi-party string to the first party + ", et al.".

    When a side of a case title lists multiple parties separated by semicolons
    or ", and" connectors, keep only the first party and append ", et al.".
    If there is only one party, return it unchanged.

    Args:
        party_side: One side (plaintiff or defendant) of a case title, after
            entity descriptors have been stripped.

    Returns:
        The truncated party string.
    """
    # Split on semicolons first (most common multi-party separator)
    parts = re.split(r"\s*;\s*", party_side)
    if len(parts) > 1:
        first = parts[0].strip().rstrip(",; ")
        if first:
            return f"{first}, et al."

    # Also try splitting on ", and " for "Alice, and Bob" style lists
    parts = re.split(r",\s+[Aa]nd\s+", party_side)
    if len(parts) > 1:
        first = parts[0].strip().rstrip(",; ")
        if first:
            return f"{first}, et al."

    return party_side


def _sanitize_title(
    raw_title: str | None,
    *,
    max_length: int | None = None,
) -> str | None:
    """Clean a raw case title: strip entity descriptors, header boilerplate, excess length.

    Returns ``None`` if the title is invalid (contains department header
    boilerplate, is too short after cleaning, or is empty).

    When a title is still too long after entity-descriptor stripping (due to
    many parties), it is truncated to "First Party, et al. v. First Party,
    et al.".  If even that exceeds ``max_length``, the title is rejected.

    Args:
        raw_title: The raw case title to clean.
        max_length: Maximum allowed title length after cleaning.  Defaults to
            ``_MAX_TITLE_LENGTH`` (120).  Callers that need a different limit
            (e.g. backfill scripts with lenient thresholds) can override this.
    """
    if max_length is None:
        max_length = _MAX_TITLE_LENGTH

    if not raw_title:
        return None

    # Reject titles that contain department header boilerplate (#1244)
    if _DEPT_HEADER_BOILERPLATE_RE.search(raw_title):
        return None

    title = raw_title

    # Normalize "vs.", "vs", "V." etc. to " v. " so splitting works consistently.
    title = re.sub(r"\s+[Vv][Ss]?\.?\s+", " v. ", title)

    # Strip entity descriptors from each side of "v."
    if " v. " in title:
        parts = title.split(" v. ", 1)
        cleaned_parts = []
        for part in parts:
            cleaned = _ENTITY_DESCRIPTOR_RE.sub("", part).strip()
            # Strip trailing semicolons, commas, "and" connectors left by removal
            cleaned = re.sub(r"[;,]\s*$", "", cleaned).strip()
            cleaned = re.sub(r"\s+And\s*$", "", cleaned, flags=re.IGNORECASE).strip()
            # Collapse "Et Al." variants
            cleaned = re.sub(
                r",?\s*Et\.?\s*Al\.?\s*$", ", et al.", cleaned, flags=re.IGNORECASE
            ).strip()
            # Remove dangling punctuation
            cleaned = cleaned.strip(",;. ")
            cleaned_parts.append(cleaned)
        title = " v. ".join(cleaned_parts)

        # If still too long, truncate multi-party sides to first party + et al.
        if len(title) > max_length:
            truncated_parts = [_truncate_party_list(p) for p in cleaned_parts]
            title = " v. ".join(truncated_parts)

    # Final length check
    if len(title) > max_length or len(title) < 5:
        return None

    return title


@dataclass
class DropdownOption:
    value: str  # raw option value, used in POST
    courthouse_code: str  # e.g. "ALH"
    courthouse: str  # e.g. "Alhambra Courthouse"
    department: str  # e.g. "3"
    hearing_date: datetime | None


class LATentativeRulingsScraper(BaseScraper):
    """Scrapes all published LA County civil tentative rulings via dropdown enumeration."""

    def __init__(
        self,
        config: ScraperConfig,
        archiver: S3Archiver | None = None,
        event_bus: EventBus | None = None,
        dept_judge_map: dict[str, str] | None = None,
        court_directory: CourtDirectory | None = None,
    ) -> None:
        super().__init__(config, archiver=archiver, event_bus=event_bus)
        self._dept_judge_map: dict[str, str] = dept_judge_map or {}
        self._court_directory = court_directory
        self._court_id: str = "ca_los_angeles"

    def fetch_documents(self) -> list[CapturedDocument]:
        # If a court directory is provided, snapshot it and use the result
        # as the department-to-judge mapping for this scraper run.
        if self._court_directory is not None:
            from courts.ca.la_dept_judges import LACourtDirectory

            self._court_id = (
                self._court_directory.COURT_ID
                if isinstance(self._court_directory, LACourtDirectory)
                else "ca_los_angeles"
            )
            self._dept_judge_map = self._court_directory.fetch_and_snapshot(self._court_id)
            self._log.info(
                "Snapshotted court directory",
                court_id=self._court_id,
                departments=len(self._dept_judge_map),
            )
        docs = []
        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            self._log.info("Fetching main page", url=CIVIL_URL)
            response = client.get(CIVIL_URL)
            response.raise_for_status()

            tokens = _extract_aspnet_tokens(response.text)
            options = _parse_dropdown_options(response.text)
            self._log.info("Found dropdown options", count=len(options))

            use_llm = _la_llm_enabled()
            if use_llm:
                self._log.info("LA LLM extraction enabled")

            for opt in options:
                time.sleep(self.config.request_delay_seconds)
                try:
                    ruling_html = _post_for_ruling(client, tokens, opt)
                    if _is_stale_viewstate_response(ruling_html):
                        self._log.warning(
                            "Stale ViewState error page; skipping",
                            courthouse=opt.courthouse,
                            dept=opt.department,
                            context="stale_viewstate",
                        )
                        continue

                    # LLM extraction path: send full HTML to LLM,
                    # get back structured rulings with all fields.
                    if use_llm:
                        llm_rulings = _llm_extract_rulings(ruling_html)
                        if llm_rulings is not None:
                            for ruling in llm_rulings:
                                doc = self._make_base_doc(
                                    source_url=CIVIL_URL,
                                    raw_content=ruling_html.encode("utf-8"),
                                    content_format=ContentFormat.HTML,
                                )
                                doc.courthouse = opt.courthouse
                                doc.department = opt.department
                                doc.hearing_date = opt.hearing_date
                                doc.extra["courthouse_code"] = opt.courthouse_code
                                doc.extra["dropdown_value"] = opt.value
                                # Pre-populate fields from LLM extraction
                                doc.case_number = ruling.case_number
                                doc.ruling_text = ruling.ruling_text
                                doc.case_title = ruling.case_title
                                doc.motion_type = ruling.motion_type
                                doc.outcome = ruling.outcome
                                doc.parties = ruling.parties
                                doc.extra["_llm_extracted"] = True
                                doc.extra["pre_split"] = True
                                doc.extra["ruling_index"] = ruling.ruling_index
                                docs.append(doc)
                            self._log.debug(
                                "LLM extracted rulings",
                                courthouse=opt.courthouse,
                                dept=opt.department,
                                date=str(opt.hearing_date),
                                cases=len(llm_rulings),
                            )
                            continue
                        # LLM failed — fall through to regex path
                        self._log.warning(
                            "LLM extraction failed, falling back to regex",
                            courthouse=opt.courthouse,
                            dept=opt.department,
                        )

                    # Regex extraction path (default, or LLM fallback)
                    case_htmls = _split_cases_html(ruling_html)
                    for case_html in case_htmls:
                        doc = self._make_base_doc(
                            source_url=CIVIL_URL,
                            raw_content=case_html.encode("utf-8"),
                            content_format=ContentFormat.HTML,
                        )
                        doc.courthouse = opt.courthouse
                        doc.department = opt.department
                        doc.hearing_date = opt.hearing_date
                        doc.extra["courthouse_code"] = opt.courthouse_code
                        doc.extra["dropdown_value"] = opt.value
                        docs.append(doc)
                    self._log.debug(
                        "Fetched ruling",
                        courthouse=opt.courthouse,
                        dept=opt.department,
                        date=str(opt.hearing_date),
                        cases=len(case_htmls),
                    )
                except Exception as exc:
                    self._log.error(
                        "Failed to fetch ruling",
                        courthouse=opt.courthouse,
                        dept=opt.department,
                        error=str(exc),
                    )
        return docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        if doc.extra.get("_llm_extracted"):
            # LLM-extracted docs may have some fields as None.
            # Run regex extraction as a fallback for null fields only,
            # preserving any non-null LLM-extracted values (#1938).
            _apply_regex_fallback_for_llm_doc(doc, self._log)
        else:
            try:
                soup = BeautifulSoup(doc.raw_content, "lxml")
                _extract_ruling_fields(soup, doc)
            except Exception as exc:
                self._log.warning("Parse error", error=str(exc))

        # Fallback: if ruling text didn't contain a judge name, try the
        # department-to-judge mapping from the judicial officer directory.
        # When a court directory is available and the ruling has a hearing
        # date, use the historical snapshot closest to that date so that
        # judge-to-department assignments are accurate for past rulings.
        if doc.judge_name is None and doc.department:
            from courts.ca.la_dept_judges import lookup_judge_for_department

            effective_map = self._dept_judge_map
            if self._court_directory is not None and doc.hearing_date is not None:
                date_map = self._court_directory.get_mapping_for_date(
                    self._court_id,
                    doc.hearing_date,
                    fallback=self._dept_judge_map,
                )
                if date_map is not None:
                    effective_map = date_map

            if effective_map:
                mapped_name = lookup_judge_for_department(effective_map, doc.department)
                if mapped_name:
                    doc.judge_name = mapped_name
                    self._log.debug(
                        "Judge name populated from department mapping",
                        department=doc.department,
                        judge_name=mapped_name,
                    )

        return doc


# ---------------------------------------------------------------------------
# ASP.NET helpers
# ---------------------------------------------------------------------------


def _extract_aspnet_tokens(html: str) -> dict[str, str]:
    """Extract __VIEWSTATE, __VIEWSTATEGENERATOR, __EVENTVALIDATION."""
    soup = BeautifulSoup(html, "lxml")
    tokens: dict[str, str] = {}
    for field in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        el = soup.find("input", {"name": field})
        if el:
            tokens[field] = el.get("value", "")
    return tokens


def _parse_dropdown_options(html: str) -> list[DropdownOption]:
    """Parse all dropdown options. Returns one DropdownOption per (courthouse, dept, date)."""
    soup = BeautifulSoup(html, "lxml")
    select = soup.find("select", {"id": SELECT_ID}) or soup.find("select", {"name": SELECT_NAME})
    if not select:
        logger.warning("Dropdown select not found")
        return []

    options = []
    for opt_el in select.find_all("option"):
        value = opt_el.get("value", "").strip()
        text = opt_el.get_text(strip=True)
        if not value:
            continue
        opt = _parse_option(value, text)
        if opt:
            options.append(opt)
    return options


def _parse_option(value: str, text: str) -> DropdownOption | None:
    """Parse a single dropdown option from its value and display text.

    Value format: "ALH,3,03/02/2026"
    Text format:  "(Alhambra Courthouse:  Dept. 3) March 2, 2026"
    """
    vm = _OPTION_VALUE_RE.match(value)
    if not vm:
        logger.debug("Unparseable option value", value=value)
        return None

    courthouse_code = vm.group("courthouse_code").strip()
    department = vm.group("department").strip()
    date_str = vm.group("date")  # MM/DD/YYYY

    hearing_date: datetime | None = None
    try:
        hearing_date = datetime.strptime(date_str, "%m/%d/%Y")
    except ValueError:
        pass

    # Courthouse name from display text (more readable than code)
    courthouse = courthouse_code
    tm = _OPTION_TEXT_RE.match(text)
    if tm:
        courthouse = tm.group("courthouse").strip()

    return DropdownOption(
        value=value,
        courthouse_code=courthouse_code,
        courthouse=courthouse,
        department=department,
        hearing_date=hearing_date,
    )


def _post_for_ruling(
    client: httpx.Client,
    tokens: dict[str, str],
    option: DropdownOption,
) -> str:
    """POST the ASP.NET form for one dropdown selection and return response HTML."""
    form_data = {
        "__VIEWSTATE": tokens.get("__VIEWSTATE", ""),
        "__VIEWSTATEGENERATOR": tokens.get("__VIEWSTATEGENERATOR", ""),
        "__EVENTVALIDATION": tokens.get("__EVENTVALIDATION", ""),
        SELECT_NAME: option.value,
        # submit2 is the named submit button on the page; server accepts it for both searches
        "submit2": "Search",
    }
    response = client.post(CIVIL_URL, data=form_data)
    response.raise_for_status()
    return response.text


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


def _is_stale_viewstate_response(html: str) -> bool:
    """Return True if the response is a stale-ViewState error page.

    LA Court returns HTTP 200 with this error page when the posted ViewState
    or dropdown option no longer matches the live page state. The page contains
    no div#speechSynthesis and is ~8KB of boilerplate error HTML.
    """
    return _LA_ERROR_MARKER in html


def _is_dept_header_boilerplate(html: str) -> bool:
    """Return True if *html* is a department header boilerplate page.

    These pages contain text like "DEPARTMENT 15 LAW AND MOTION RULINGS"
    but no actual ruling content (no ``Case Number:`` present).  They
    should not be stored as ruling rows.

    Uses a lightweight regex tag-strip instead of a full BeautifulSoup
    parse — this is called once per department response and only needs
    to check for two simple patterns.
    """
    text = _HTML_TAG_RE.sub("", html)
    if _CASE_NUMBER_RE.search(text):
        return False
    return bool(_DEPT_HEADER_BOILERPLATE_RE.search(text))


def _split_cases_html(ruling_html: str) -> list[str]:
    """Split a department response HTML into per-case HTML sections.

    A single department response may contain rulings for multiple cases.
    Each case section is preceded by a ``<HR>`` tag and a
    ``<B> Case Number: </B>`` header.

    Returns a list of HTML strings, each wrapped in a
    ``<div id="speechSynthesis">`` so downstream parsing works unchanged.
    Returns an empty list when no valid case sections are found (e.g.
    department header boilerplate pages with no ``Case Number:`` present).

    Results are cached by input HTML to avoid redundant BeautifulSoup
    parses when the same department response is processed multiple times
    (e.g. during concurrent scraper runs or test loops).
    """
    # Delegate to the cached implementation (returns a tuple for hashability).
    return list(_split_cases_html_cached(ruling_html))


@functools.lru_cache(maxsize=256)
def _split_cases_html_cached(ruling_html: str) -> tuple[str, ...]:
    """Cached implementation of _split_cases_html.

    Returns a tuple (hashable for lru_cache).  The public wrapper
    converts back to a list for API compatibility.
    """
    soup = BeautifulSoup(ruling_html, "lxml")
    speech_div = soup.find("div", id="speechSynthesis")
    if speech_div is None:
        return ()

    inner_html = speech_div.decode_contents()

    # Split on <HR> immediately before a Case Number header.
    sections = _CASE_SPLIT_RE.split(inner_html)

    # Filter out empty/whitespace-only sections and the department header
    # that appears before the first case number.
    result: list[str] = []
    for section in sections:
        stripped = section.strip()
        if not stripped:
            continue
        # A section is valid only if it contains a Case Number header.
        # Use a lightweight regex tag-strip instead of a full BeautifulSoup
        # parse — this is called once per section per department (hundreds
        # of times in a full run) and the tag-strip only needs to be
        # accurate enough for the "Case Number:" substring check.
        section_text = _HTML_TAG_RE.sub("", stripped)
        if not _CASE_NUMBER_RE.search(section_text):
            continue
        # Wrap each section back in the speechSynthesis div and a minimal
        # HTML shell so BeautifulSoup parsing in _extract_ruling_fields works.
        wrapped = f'<html><body><div id="speechSynthesis">{stripped}</div></body></html>'
        result.append(wrapped)

    # If splitting produced nothing, the response likely contains only
    # department header boilerplate (e.g. "DEPARTMENT 15 LAW AND MOTION
    # RULINGS") with no actual rulings.  Return an empty list so these
    # pages are not stored as ruling rows.
    if not result:
        return ()

    return tuple(result)


def _apply_regex_fallback_for_llm_doc(
    doc: CapturedDocument,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Apply regex extraction as fallback for null fields on LLM-extracted docs.

    When the LLM extracts a ruling but leaves some fields as ``None``, we
    run the regex-based ``_extract_ruling_fields()`` on the correct per-case
    HTML chunk and use its results to fill in only the missing fields.
    Non-null LLM values are always preserved.

    For multi-case department pages, ``_split_cases_html()`` splits the full
    HTML into per-case chunks, and the ``ruling_index`` stored in
    ``doc.extra`` selects the correct chunk.  This prevents cross-case
    contamination (e.g. extracting the first case's title for the second
    case's document).  If the correct chunk cannot be identified, the
    fallback is aborted to avoid data corruption.
    """
    fallback_fields = ("case_number", "case_title", "ruling_text", "judge_name")
    needs_fallback = any(getattr(doc, f) is None for f in fallback_fields)
    needs_party_fallback = not doc.parties

    if not needs_fallback and not needs_party_fallback:
        log.debug(
            "LLM doc has all fields; skipping regex fallback",
            case_number=doc.case_number,
        )
        return

    # Determine the correct HTML chunk for this specific ruling.
    # Always use _split_cases_html() to get clean per-case chunks,
    # then select the right one via ruling_index.
    case_htmls = _split_cases_html(doc.raw_content.decode("utf-8", "ignore"))

    if not case_htmls:
        log.warning(
            "Cannot apply regex fallback: no case chunks found",
            case_number=doc.case_number,
        )
        return

    # Default to index 1 for single-case pages where ruling_index
    # may not be explicitly set.
    ruling_index = doc.extra.get("ruling_index", 1)
    if not (isinstance(ruling_index, int) and 1 <= ruling_index <= len(case_htmls)):
        log.warning(
            "Cannot apply regex fallback: ruling_index out of bounds",
            case_number=doc.case_number,
            ruling_index=ruling_index,
            num_chunks=len(case_htmls),
        )
        return

    html_content = case_htmls[ruling_index - 1].encode("utf-8")
    log.debug(
        "Using ruling_index to select HTML chunk for fallback",
        ruling_index=ruling_index,
        num_chunks=len(case_htmls),
    )

    # Save the complete original state of all modifiable fields so we
    # can restore to the exact pre-call state if regex extraction fails.
    all_restorable = list(fallback_fields) + ["parties"]
    original_state: dict[str, object] = {f: getattr(doc, f, None) for f in all_restorable}

    # Also track which LLM-extracted values are non-null so we can
    # restore them after a successful regex extraction (LLM takes precedence).
    saved: dict[str, object] = {}
    for f in fallback_fields:
        val = getattr(doc, f)
        if val is not None:
            saved[f] = val
    if doc.parties:
        saved["parties"] = doc.parties

    try:
        soup = BeautifulSoup(html_content, "lxml")
        _extract_ruling_fields(soup, doc)
    except Exception as exc:
        log.warning("Regex fallback for LLM doc failed", error=str(exc))
        # Restore document to its exact pre-call state to avoid
        # partial/corrupt data from a failed regex extraction.
        for f, val in original_state.items():
            setattr(doc, f, val)
        return

    # Restore non-null LLM values (they take precedence over regex).
    for f, val in saved.items():
        setattr(doc, f, val)

    log.debug(
        "Applied regex fallback for LLM doc",
        case_number=doc.case_number,
        backfilled=[f for f in fallback_fields if f not in saved],
    )


def _extract_ruling_fields(soup: BeautifulSoup, doc: CapturedDocument) -> None:
    """Extract structured fields from a single-case ruling HTML.

    The ruling content lives in div#speechSynthesis.  Multi-case department
    responses are split into individual sections by ``_split_cases_html``
    before this function is called, so each invocation handles exactly one case.
    """
    content = soup.find("div", id="speechSynthesis")
    if not content:
        # Fallback: use full body text
        doc.ruling_text = soup.get_text(separator="\n", strip=True)
        return

    full_text = content.get_text(separator="\n", strip=True)
    doc.ruling_text = full_text

    # All case numbers in this response
    case_numbers = _CASE_NUMBER_RE.findall(full_text)
    if case_numbers:
        doc.case_number = case_numbers[0]
        if len(case_numbers) > 1:
            doc.extra["all_case_numbers"] = case_numbers

    # Case title from the party caption block
    doc.case_title = _extract_case_title(content)

    # Party extraction
    doc.parties = _extract_parties(content)

    # Judge name from the signature div
    for div in content.find_all("div"):
        div_text = div.get_text(separator=" ", strip=True)
        m = _JUDGE_DIV_RE.match(div_text)
        if m:
            # Normalize whitespace in name
            doc.judge_name = " ".join(m.group(1).split())
            break

    # Fallback: use the broader regex patterns from extract.py
    if not doc.judge_name and doc.ruling_text:
        from ingestion.extract import extract_judge_name

        doc.judge_name = extract_judge_name(doc.ruling_text)


def _extract_case_title(content: BeautifulSoup) -> str | None:
    """Extract the first case title from ruling HTML content.

    Tries multiple extraction strategies in order of reliability:

    1. ``<a name="Parties">`` anchor with formal caption block (most reliable)
    2. Inline "Case Name:" or "Case Title:" field
    3. "MOVING PARTY:" / "RESPONDING PARTY:" fields

    All results are passed through ``_sanitize_title`` to strip entity
    descriptors and reject titles containing department header boilerplate.
    """
    # Strategy 1: Parties anchor with formal caption block
    title = _extract_title_from_parties_anchor(content)
    sanitized = _sanitize_title(title)
    if sanitized is not None:
        return sanitized

    # For fallback strategies, work with the full text content
    full_text = content.get_text(separator="\n", strip=True)

    # Strategy 2: Inline "Case Name:" / "Case Title:" field
    title = _extract_title_from_case_name_field(full_text)
    sanitized = _sanitize_title(title)
    if sanitized is not None:
        return sanitized

    # Strategy 3: MOVING PARTY / RESPONDING PARTY fields
    title = _extract_title_from_moving_responding(full_text)
    return _sanitize_title(title)


def _extract_title_from_parties_anchor(content: BeautifulSoup) -> str | None:
    """Extract case title from the ``<a name="Parties">`` anchor pattern."""
    anchor = content.find("a", attrs={"name": "Parties"})
    if anchor is None:
        return None

    # Walk up to the enclosing <td>
    td = anchor.find_parent("td")
    if td is None:
        return None

    td_text = td.get_text(separator="\n", strip=False)
    m = _CASE_TITLE_RE.search(td_text)
    if m is None:
        return None

    plaintiff = _clean_party_name(m.group("plaintiff"))
    defendant = _clean_party_name(m.group("defendant"))

    if not plaintiff or not defendant:
        return None

    return f"{plaintiff.title()} v. {defendant.title()}"


def _clean_party_name(raw: str) -> str:
    """Normalise a captured party name: collapse whitespace, strip role prefix,
    strip trailing commas/et al, and clean up punctuation.

    Returns an empty string if the result is a bare role label (e.g.
    "Defendant", "Plaintiffs") with no actual litigant name.
    """
    name = " ".join(raw.split()).strip()
    # Strip role prefixes like "Defendant " or "Defendant, " or "Plaintiffs "
    name = _ROLE_PREFIX_RE.sub("", name)
    # Strip "et al." suffix
    name = re.sub(r",?\s*et\s+al\.?\s*$", "", name, flags=re.IGNORECASE).strip()
    # Remove stray leading/trailing punctuation
    name = name.strip(")(,.; ")
    # Reject bare role labels that slipped through (e.g. captured text was
    # just "Defendant" with no actual party name following it).
    if name.lower() in _BARE_ROLE_LABELS:
        return ""
    return name


def _extract_title_from_moving_responding(text: str) -> str | None:
    """Extract a case title from MOVING PARTY / RESPONDING PARTY fields."""
    m_match = _MOVING_PARTY_RE.search(text)
    if m_match is None:
        return None
    r_match = _RESPONDING_PARTY_RE.search(text)
    if r_match is None:
        return None

    moving_raw = m_match.group("name").strip()
    responding_raw = r_match.group("name").strip()

    # Reject non-party content like "No opposition filed"
    skip_phrases = _SKIP_RESPONDING_PHRASES
    for phrase in skip_phrases:
        if phrase in responding_raw.lower():
            return None

    moving_name = _clean_party_name(moving_raw)
    responding_name = _clean_party_name(responding_raw)

    if not moving_name or not responding_name:
        return None

    title = f"{moving_name.title()} v. {responding_name.title()}"

    if len(title) > 150:
        return None

    return title


def _extract_title_from_case_name_field(text: str) -> str | None:
    """Extract a case title from an inline 'Case Name:' or 'Case Title:' field.

    In HTML-derived text, individual characters may be on separate lines
    (due to deeply nested spans). We collapse all whitespace into spaces
    before searching.
    """
    # Collapse multi-line text into a single line for matching
    collapsed = " ".join(text.split())

    m = _CASE_NAME_FIELD_RE.search(collapsed)
    if m is None:
        return None

    raw_title = m.group("title").strip()

    # Must contain "v." or "v " to be a real case name
    if not re.search(r"\bv\.?\s", raw_title):
        return None

    # Clean up whitespace
    title = " ".join(raw_title.split())

    # Fix "v ." -> "v." (HTML fragmentation artifact)
    title = re.sub(r"\bv\s+\.", "v.", title)

    # Strip trailing punctuation
    title = title.rstrip(".,;: ")

    if len(title) > 150 or len(title) < 5:
        return None

    return title


# ---------------------------------------------------------------------------
# Party extraction
# ---------------------------------------------------------------------------


def _extract_parties(content: BeautifulSoup) -> list[dict[str, str]]:
    """Extract party names and roles from ruling HTML content.

    Tries multiple extraction strategies in order of reliability:

    1. ``<a name="Parties">`` anchor with formal caption block (most reliable)
    2. "MOVING PARTY:" / "RESPONDING PARTY:" fields (fallback)

    Returns a list of dicts, each with ``name`` and ``role`` keys.
    """
    parties = _extract_parties_from_anchor(content)
    if parties:
        return parties

    full_text = content.get_text(separator="\n", strip=True)
    return _extract_parties_from_moving_responding(full_text)


def _extract_parties_from_anchor(content: BeautifulSoup) -> list[dict[str, str]]:
    """Extract party records from ``<a name="Parties">`` anchor blocks.

    Finds all Parties anchors in the content (one per case in multi-case
    responses), parses the enclosing ``<td>`` text to identify plaintiff/
    defendant names and their roles.
    """
    parties: list[dict[str, str]] = []
    seen_names: set[str] = set()

    for anchor in content.find_all("a", attrs={"name": "Parties"}):
        td = anchor.find_parent("td")
        if td is None:
            continue

        td_text = td.get_text(separator="\n", strip=False)
        m = _CASE_PARTIES_RE.search(td_text)
        if m is None:
            continue

        p_role = _ROLE_MAP.get(m.group("p_role").lower(), "plaintiff")
        d_role = _ROLE_MAP.get(m.group("d_role").lower(), "defendant")

        plaintiff_raw = " ".join(m.group("plaintiff").split()).strip().rstrip(",")
        defendant_raw = " ".join(m.group("defendant").split()).strip().rstrip(",")

        plaintiff_name = _clean_party_name(plaintiff_raw)
        defendant_name = _clean_party_name(defendant_raw)

        if plaintiff_name:
            key = plaintiff_name.lower()
            if key not in seen_names:
                seen_names.add(key)
                parties.append({"name": plaintiff_name.title(), "role": p_role})

        if defendant_name:
            key = defendant_name.lower()
            if key not in seen_names:
                seen_names.add(key)
                parties.append({"name": defendant_name.title(), "role": d_role})

    return parties


def _extract_parties_from_moving_responding(text: str) -> list[dict[str, str]]:
    """Extract party records from MOVING PARTY / RESPONDING PARTY fields.

    Uses ``moving_party`` and ``responding_party`` as roles since the
    actual plaintiff/defendant designation is unclear from these fields.
    Individual names are split on " and " when multiple are listed.
    """
    m_match = _MOVING_PARTY_RE.search(text)
    if m_match is None:
        return []
    r_match = _RESPONDING_PARTY_RE.search(text)
    if r_match is None:
        return []

    moving_raw = m_match.group("name").strip()
    responding_raw = r_match.group("name").strip()

    # Reject non-party content like "No opposition filed"
    skip_phrases = _SKIP_RESPONDING_PHRASES
    for phrase in skip_phrases:
        if phrase in responding_raw.lower():
            return []

    parties: list[dict[str, str]] = []
    seen_names: set[str] = set()

    for raw_name, role in [
        (moving_raw, "moving_party"),
        (responding_raw, "responding_party"),
    ]:
        # Split on " and " to get individual names when multiple are listed
        # e.g. "Plaintiffs David Keichline, Claudia Lopez, and Mason Keichline"
        cleaned = _clean_party_name(raw_name)
        if not cleaned:
            continue

        # Try splitting on ", and " or " and " for multiple names
        # But be careful: "Ashley Willowbrook LP and Ashley Willowbrook GP LP"
        # is two separate entities, while "David Keichline, Claudia Lopez, and
        # Mason Keichline" is three people.
        sub_names = _split_party_names(cleaned)
        for name in sub_names:
            name = name.strip().strip(")(,.; ")
            if not name:
                continue
            key = name.lower()
            if key not in seen_names:
                seen_names.add(key)
                parties.append({"name": name.title(), "role": role})

    return parties


# _is_name_fragment and _split_party_names are imported from
# framework.party_utils above.  They were previously defined inline here
# (see #404 for deduplication rationale).


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def default_config(s3_bucket: str = "") -> ScraperConfig:
    from datetime import time as dtime

    from framework import ScheduleWindow

    return ScraperConfig(
        scraper_id="ca-la-tentatives-civil",
        state="CA",
        county="Los Angeles",
        court="Superior Court",
        target_urls=[CIVIL_URL],
        poll_interval_seconds=43200,  # twice daily
        schedule_windows=[
            ScheduleWindow(start=dtime(18, 0), end=dtime(19, 0)),  # 6 PM sweep
            ScheduleWindow(start=dtime(2, 0), end=dtime(3, 0)),  # 2 AM catch-up
        ],
        request_delay_seconds=1.5,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
