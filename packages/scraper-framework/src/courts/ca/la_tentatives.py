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

import re
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
import structlog
from bs4 import BeautifulSoup

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig
from framework.events import EventBus
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

# Like _CASE_TITLE_RE but also captures the role keywords so we can map names to roles.
_CASE_PARTIES_RE = re.compile(
    r"^(?P<plaintiff>.+?),?\s*\n\s*(?P<p_role>Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?,?"
    r"\s+vs\.\s+"
    r"(?P<defendant>.+?),?\s*\n\s*(?P<d_role>Defendant|Respondent|Cross-Defendant)\(?s?\)?\.?",
    re.DOTALL | re.MULTILINE,
)

# Map caption role keywords to normalized role values for the case_parties table.
_ROLE_MAP: dict[str, str] = {
    "plaintiff": "plaintiff",
    "petitioner": "petitioner",
    "cross-complainant": "cross_complainant",
    "defendant": "defendant",
    "respondent": "respondent",
    "cross-defendant": "cross_defendant",
}

# ---------------------------------------------------------------------------
# Fallback title extraction patterns (text-based, for rulings without anchors)
# ---------------------------------------------------------------------------

# Pattern 2: "MOVING PARTY: [name]" / "RESPONDING PARTY: [name]"
_MOVING_PARTY_RE = re.compile(
    r"MOVING PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)
_RESPONDING_PARTY_RE = re.compile(
    r"(?:RESPONDING|OPPOSING) PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)
_ROLE_PREFIX_RE = re.compile(
    r"^(?:Defendants?|Plaintiffs?|Petitioners?|Respondents?"
    r"|Cross-Complainants?|Cross-Defendants?)\s+",
    re.IGNORECASE,
)

# Pattern 3: "Case Name: [text]" or "Case Title: [text]"
_CASE_NAME_FIELD_RE = re.compile(
    r"CASE\s+(?:NAME|TITLE)\s*:\s*(?P<title>.+?)(?:\s+CASE\s+NUMBER|\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

# Marker present in the LA Court stale-ViewState error page (HTTP 200, ~8KB).
# When the POST uses an expired ViewState or a dropdown option that no longer
# exists, the server returns this error page instead of ruling content.
_LA_ERROR_MARKER = "We're sorry"


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
    ) -> None:
        super().__init__(config, archiver=archiver, event_bus=event_bus)
        self._dept_judge_map: dict[str, str] = dept_judge_map or {}

    def fetch_documents(self) -> list[CapturedDocument]:
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
        try:
            soup = BeautifulSoup(doc.raw_content, "lxml")
            _extract_ruling_fields(soup, doc)
        except Exception as exc:
            self._log.warning("Parse error", error=str(exc))

        # Fallback: if ruling text didn't contain a judge name, try the
        # department-to-judge mapping from the judicial officer directory.
        if doc.judge_name is None and doc.department and self._dept_judge_map:
            from courts.ca.la_dept_judges import lookup_judge_for_department

            mapped_name = lookup_judge_for_department(self._dept_judge_map, doc.department)
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


def _split_cases_html(ruling_html: str) -> list[str]:
    """Split a department response HTML into per-case HTML sections.

    A single department response may contain rulings for multiple cases.
    Each case section is preceded by a ``<HR>`` tag and a
    ``<B> Case Number: </B>`` header.

    Returns a list of HTML strings, each wrapped in a
    ``<div id="speechSynthesis">`` so downstream parsing works unchanged.
    If only one case is present, returns a single-element list containing
    the original HTML unmodified.
    """
    soup = BeautifulSoup(ruling_html, "lxml")
    speech_div = soup.find("div", id="speechSynthesis")
    if speech_div is None:
        return [ruling_html]

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
        # Check against plain text since the HTML may have tags between
        # "Case Number:" and the actual number.
        section_text = BeautifulSoup(stripped, "lxml").get_text()
        if not _CASE_NUMBER_RE.search(section_text):
            continue
        # Wrap each section back in the speechSynthesis div and a minimal
        # HTML shell so BeautifulSoup parsing in _extract_ruling_fields works.
        wrapped = f'<html><body><div id="speechSynthesis">{stripped}</div></body></html>'
        result.append(wrapped)

    # If splitting produced nothing (unexpected format), fall back to original
    if not result:
        return [ruling_html]

    return result


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
    """
    # Strategy 1: Parties anchor with formal caption block
    title = _extract_title_from_parties_anchor(content)
    if title is not None:
        return title

    # For fallback strategies, work with the full text content
    full_text = content.get_text(separator="\n", strip=True)

    # Strategy 2: Inline "Case Name:" / "Case Title:" field
    title = _extract_title_from_case_name_field(full_text)
    if title is not None:
        return title

    # Strategy 3: MOVING PARTY / RESPONDING PARTY fields
    return _extract_title_from_moving_responding(full_text)


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

    plaintiff = " ".join(m.group("plaintiff").split()).strip().rstrip(",")
    defendant = " ".join(m.group("defendant").split()).strip().rstrip(",")

    if not plaintiff or not defendant:
        return None

    return f"{plaintiff.title()} v. {defendant.title()}"


def _clean_party_name(raw: str) -> str:
    """Normalise a captured party name: collapse whitespace, strip role prefix,
    strip trailing commas/et al, and clean up punctuation."""
    name = " ".join(raw.split()).strip()
    # Strip role prefixes like "Defendant " or "Plaintiffs "
    name = _ROLE_PREFIX_RE.sub("", name)
    # Strip "et al." suffix
    name = re.sub(r",?\s*et\s+al\.?\s*$", "", name, flags=re.IGNORECASE).strip()
    # Remove stray leading/trailing punctuation
    name = name.strip(")(,.; ")
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
    skip_phrases = ("no opposition", "none", "no response", "unopposed")
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
    skip_phrases = ("no opposition", "none", "no response", "unopposed")
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


def _is_name_fragment(name: str) -> bool:
    """Return True if *name* is a fragment that should not be a standalone party.

    Rejects:
    - Corporate suffixes alone (Inc, LLC, Corp, Ltd, etc.)
    - Single words shorter than 3 characters
    - Names that look like incomplete fragments (single word, no space)
    """
    stripped = name.strip().rstrip(".,;: ")
    if not stripped:
        return True

    upper = stripped.upper().rstrip(".")
    # Standalone corporate suffixes
    corp_suffixes = {
        "INC",
        "LLC",
        "LLP",
        "LP",
        "CORP",
        "CORPORATION",
        "LTD",
        "CO",
        "COMPANY",
        "NA",
        "PC",
        "PLLC",
        "PLC",
    }
    if upper in corp_suffixes:
        return True

    # Single word with no space — likely a fragment (first name only, etc.)
    # Allow single-word org names that are long enough (e.g. "Google")
    if " " not in stripped and len(stripped) < 4:
        return True

    return False


# Corporate suffix patterns that should NOT trigger a comma split.
# Matches ", Inc", ", LLC", etc. at the end of a name or before another comma.
_CORP_SUFFIX_RE = re.compile(
    r",\s*(?:Inc|LLC|LLP|L\.?P\.?|Corp|Corporation|Ltd|Co|Company"
    r"|N\.?A\.?|P\.?C\.?|PLLC|PLC)\.?(?=\s*(?:,|$))",
    re.IGNORECASE,
)


def _split_party_names(text: str) -> list[str]:
    """Split a string containing multiple party names into individual names.

    Handles patterns like:
    - "David Keichline, Claudia Lopez, and Mason Keichline"
    - "Ashley Willowbrook LP and Ashley Willowbrook GP LP"
    - "Techno-Advanced, Inc." (corporate suffix kept with name)

    Uses ", " as the primary delimiter. Also splits on " and " when it
    appears after a comma-separated list (Oxford comma pattern).

    Corporate suffixes (Inc, LLC, Corp, etc.) preceded by commas are
    protected from splitting so "Techno-Advanced, Inc." stays intact.
    """
    # Protect corporate suffixes from comma-splitting by replacing the comma
    # with a placeholder.  E.g. "Techno-Advanced, Inc." -> "Techno-Advanced\x00 Inc."
    placeholder = "\x00"
    protected = _CORP_SUFFIX_RE.sub(lambda m: m.group(0).replace(",", placeholder, 1), text)

    # First, handle Oxford comma: "A, B, and C" -> split on ", " and ", and "
    parts = re.split(r",\s+and\s+|,\s+", protected)
    # If no commas found, try splitting on standalone " and "
    if len(parts) == 1:
        parts = re.split(r"\s+and\s+", protected)

    # Restore placeholders and filter fragments
    result: list[str] = []
    for p in parts:
        restored = p.replace(placeholder, ",").strip()
        if restored and not _is_name_fragment(restored):
            result.append(restored)
    return result


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
