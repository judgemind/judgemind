"""San Diego Superior Court — Odyssey ROA Tentative Rulings Scraper (Phase 2).

Strategy: For each case number from the Phase 1 calendar scraper, use Playwright
to navigate the Odyssey ROA portal and extract tentative rulings. The portal is
protected by Cloudflare Bot Management, requiring a real browser with stealth mode.

This scraper:
1. Launches a Playwright browser (Chromium) with stealth settings
2. Solves the Cloudflare challenge once per session
3. For each case number, searches via SmartSearch and navigates to the ROA
4. Extracts tentative ruling content and structured fields
5. Archives raw HTML to S3

Verified against fixtures based on Tyler Odyssey ROA standard patterns.
  Portal: https://odyroa.sdcourt.ca.gov/portal/
  SmartSearch: https://odyroa.sdcourt.ca.gov/portal/Home/SmartSearch?searchString={caseNumber}
  CaseDetail: https://odyroa.sdcourt.ca.gov/portal/Home/CaseDetail/{caseId}

Cloudflare challenge:
  - cf_clearance cookie valid ~30 minutes after solving
  - Stealth mode required to pass JS challenge
  - Residential proxy may be needed for datacenter IPs

Investigation: #154
Parent issue: #672
Report: docs/investigations/san-diego-scraper-2026-03.md
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from bs4 import BeautifulSoup

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig
from framework.events import EventBus
from framework.storage import S3Archiver

logger = structlog.get_logger(__name__)

PORTAL_BASE_URL = "https://odyroa.sdcourt.ca.gov"
SMART_SEARCH_URL = f"{PORTAL_BASE_URL}/portal/Home/SmartSearch"

# Default delay between requests (seconds) to avoid triggering rate limits.
DEFAULT_REQUEST_DELAY = 3.0

# Maximum time (seconds) to wait for Cloudflare challenge to resolve per attempt.
CF_CHALLENGE_TIMEOUT = 45.0

# Maximum time (seconds) to wait for page navigation.
PAGE_LOAD_TIMEOUT = 60000  # Playwright uses milliseconds

# Number of retry attempts for Cloudflare challenge resolution.
CF_MAX_RETRIES = 3

# Seconds between cf_clearance cookie poll checks during challenge resolution.
CF_POLL_INTERVAL = 2.0

# ---------------------------------------------------------------------------
# LLM extraction — feature flag and helpers (#2056)
# ---------------------------------------------------------------------------

# Default LLM provider and model for San Diego supplemental extraction.
_SD_LLM_PROVIDER = "google"
_SD_LLM_MODEL = "gemini-2.5-flash-lite"

# Outcome mapping: LLM taxonomy -> DB enum values.
# The SD LLM prompt uses fine-grained demurrer outcomes (sustained,
# sustained_with_leave, overruled) that are not in the DB enum.
# Map them to the standard enum values.
_SD_OUTCOME_MAP: dict[str | None, str | None] = {
    "granted": "granted",
    "denied": "denied",
    "granted_in_part": "granted_in_part",
    "denied_in_part": "denied_in_part",
    "sustained": "granted",
    "sustained_with_leave": "granted",
    "overruled": "denied",
    "moot": "moot",
    "continued": "continued",
    "off_calendar": "off_calendar",
    "submitted": "submitted",
    "other": "other",
    None: None,
}


def _sd_llm_enabled() -> bool:
    """Return True when LLM-based extraction is enabled for San Diego rulings."""
    return os.environ.get("ENABLE_SD_LLM_EXTRACTION", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _sd_llm_extract(
    ruling_text: str,
    case_number: str | None = None,
) -> dict[str, Any] | None:
    """Extract outcome and motion_type from San Diego ruling text using an LLM.

    Sends the ruling text (already extracted from the ROA HTML by BeautifulSoup)
    plus known metadata (case_number) to the LLM with the San Diego-specific
    system prompt.  Returns a dict with keys ``outcome`` and ``motion_type``
    on success, or ``None`` if the LLM call fails or the response cannot be
    parsed.

    The LLM supplements the scraper's HTML-based extraction by providing more
    accurate outcome classification (especially for mixed demurrer rulings)
    and more specific motion_type descriptions than the regex keyword list.
    """
    from framework.extraction_config import SAN_DIEGO_SYSTEM_PROMPT
    from ingestion.llm_providers import call_llm

    if not ruling_text or not ruling_text.strip():
        return None

    # Build user message with known metadata context.
    parts: list[str] = []
    meta_lines: list[str] = []
    if case_number:
        meta_lines.append(f"Case number: {case_number}")
    if meta_lines:
        parts.append("Known metadata:\n" + "\n".join(meta_lines))
    parts.append(f"Ruling text:\n\n{ruling_text}")
    user_message = "\n\n".join(parts)

    response = call_llm(
        system_prompt=SAN_DIEGO_SYSTEM_PROMPT,
        user_message=user_message,
        provider=_SD_LLM_PROVIDER,
        model=_SD_LLM_MODEL,
        max_tokens=4096,
        timeout=60.0,
    )

    if response is None:
        logger.warning("sd.llm_extraction_failed", reason="null_response", case_number=case_number)
        return None

    try:
        raw = response.text.strip()
        # Extract JSON object by finding the first { and last }.
        json_start = raw.find("{")
        json_end = raw.rfind("}")
        if json_start != -1 and json_end != -1 and json_end > json_start:
            raw = raw[json_start : json_end + 1]

        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("sd.llm_parse_failed", error=str(exc), case_number=case_number)
        return None

    if not isinstance(data, dict):
        logger.warning(
            "sd.llm_unexpected_shape",
            shape=type(data).__name__,
            case_number=case_number,
        )
        return None

    # Normalize outcome using the mapping (case-insensitive).
    # Only process string values; any other type (list, dict, int) maps to None.
    raw_outcome = data.get("outcome")
    outcome_key: str | None = None
    if isinstance(raw_outcome, str):
        outcome_key = raw_outcome.lower()
    outcome = _SD_OUTCOME_MAP.get(outcome_key)

    result: dict[str, Any] = {
        "outcome": outcome,
        "motion_type": data.get("motion_type"),
    }

    logger.info(
        "sd.llm_extraction_success",
        outcome=outcome,
        motion_type=result["motion_type"],
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )

    return result


# ---------------------------------------------------------------------------
# ROA HTML parsing
# ---------------------------------------------------------------------------

# Case number from the case header: "Case No: 24CU016153C"
_CASE_NUMBER_RE = re.compile(r"Case\s+No:\s*(?P<case_number>\S+)")

# Judge name: "Honorable Matthew C. Braner"
_JUDGE_RE = re.compile(
    r"Honorable\s+(?P<judge_name>[A-Z][^\n,]+?)(?:,?\s+Presiding)?$",
    re.IGNORECASE | re.MULTILINE,
)

# Department: "C-60", "N-28", etc. in a value cell
_DEPARTMENT_RE = re.compile(r"^(?P<department>[A-Z]-\d+|\d+)$")

# Hearing date in ruling text: "Hearing Date: March 13, 2026"
_HEARING_DATE_RE = re.compile(
    r"Hearing\s+Date:\s*(?P<date>"
    r"(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

# Outcome keywords
_OUTCOME_RE = re.compile(
    r"\b(?P<outcome>"
    r"GRANTED(?:\s+IN\s+PART(?:\s+AND\s+DENIED\s+IN\s+PART)?)?"
    r"|DENIED(?:\s+IN\s+PART)?"
    r"|SUSTAINED(?:\s+WITH\s+LEAVE\s+TO\s+AMEND)?"
    r"|OVERRULED"
    r"|MOOT"
    r"|OFF\s+CALENDAR"
    r")\b",
    re.IGNORECASE,
)

# Motion type keywords
_MOTION_TYPE_RE = re.compile(
    r"\b(?P<motion_type>"
    r"Demurrer(?:\s+to\s+\w+)?"
    r"|Motion\s+to\s+(?:Compel(?:\s+Further\s+Responses)?|Dismiss|Strike|Quash"
    r"|Stay|Vacate|Set\s+Aside|Tax\s+Costs)"
    r"|Motion\s+for\s+(?:Summary\s+(?:Judgment|Adjudication)|Sanctions"
    r"|New\s+Trial|Judgment\s+on\s+the\s+Pleadings)"
    r"|Summary\s+(?:Judgment|Adjudication)"
    r"|Petition\s+to\s+Compel\s+Arbitration"
    r"|Writ\s+of\s+Attachment"
    r"|Preliminary\s+Injunction"
    r")\b",
    re.IGNORECASE,
)

# Party role in the party table
_PARTY_TYPE_RE = re.compile(r"^(?P<type>Plaintiff|Defendant|Petitioner|Respondent)$", re.IGNORECASE)

# Role mapping from party type to normalized role.
_ROLE_MAP: dict[str, str] = {
    "plaintiff": "plaintiff",
    "defendant": "defendant",
    "petitioner": "plaintiff",
    "respondent": "defendant",
}


@dataclass
class ROACaseInfo:
    """Structured data parsed from an Odyssey ROA case page."""

    case_number: str | None = None
    case_title: str | None = None
    judge_name: str | None = None
    department: str | None = None
    hearing_date: datetime | None = None
    ruling_text: str | None = None
    outcome: str | None = None
    motion_type: str | None = None
    parties: list[dict[str, str]] = field(default_factory=list)
    has_tentative_ruling: bool = False


def is_cloudflare_challenge(html: str) -> bool:
    """Return True if the HTML appears to be a Cloudflare challenge page."""
    indicators = [
        "window._cf_chl_opt",
        "challenge-platform",
        "Just a moment",
        "cf-challenge-running",
        "/cdn-cgi/challenge-platform/",
    ]
    return any(indicator in html for indicator in indicators)


async def has_cf_clearance(context: Any) -> bool:
    """Check whether the browser context has a valid cf_clearance cookie.

    Cloudflare sets this cookie after a challenge is solved. Its presence
    indicates the challenge has been passed and subsequent requests within
    the same session will not be re-challenged (for ~30 minutes).
    """
    cookies = await context.cookies()
    return any(c.get("name") == "cf_clearance" for c in cookies)


async def _apply_stealth(page: Any) -> None:
    """Apply playwright-stealth evasions to a page to avoid bot detection.

    Uses the ``playwright-stealth`` library to mask browser automation
    fingerprints (WebGL, navigator properties, plugins, etc.) that
    Cloudflare uses for bot detection.

    Falls back gracefully if playwright-stealth is not installed, applying
    only the minimal webdriver override.
    """
    try:
        from playwright_stealth import Stealth

        stealth = Stealth()
        await stealth.apply_stealth_async(page)
    except ImportError:
        logger.warning("playwright-stealth not installed, using minimal stealth only")
        # Minimal fallback: remove webdriver flag
        await page.evaluate("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")


def parse_case_header(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """Extract case number and title from the case header section.

    Returns (case_number, case_title).
    """
    case_number: str | None = None
    case_title: str | None = None

    # Case number
    case_num_span = soup.find("span", class_="case-number")
    if case_num_span:
        text = case_num_span.get_text(strip=True)
        m = _CASE_NUMBER_RE.search(text)
        if m:
            case_number = m.group("case_number")

    # Case title
    case_title_span = soup.find("span", class_="case-title")
    if case_title_span:
        case_title = case_title_span.get_text(strip=True)

    return case_number, case_title


def parse_case_details(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """Extract judge name and department from the case info table.

    Returns (judge_name, department).
    """
    judge_name: str | None = None
    department: str | None = None

    info_table = soup.find("table", class_="case-info-table")
    if not info_table:
        return judge_name, department

    for row in info_table.find_all("tr"):
        label_td = row.find("td", class_="label")
        value_td = row.find("td", class_="value")
        if not label_td or not value_td:
            continue

        label = label_td.get_text(strip=True).rstrip(":")
        value = value_td.get_text(strip=True)

        if label == "Judge":
            m = _JUDGE_RE.search(value)
            if m:
                judge_name = " ".join(m.group("judge_name").strip().split())
        elif label == "Department":
            department = value.strip()

    return judge_name, department


def parse_parties(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Extract party information from the party table."""
    parties: list[dict[str, str]] = []
    seen: set[str] = set()

    party_table = soup.find("table", class_="party-table")
    if not party_table:
        return parties

    tbody = party_table.find("tbody")
    if not tbody:
        return parties

    for row in tbody.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) < 2:
            continue

        party_type = tds[0].get_text(strip=True).lower()
        name = tds[1].get_text(strip=True)

        if not name:
            continue

        role = _ROLE_MAP.get(party_type, party_type)
        key = name.lower()
        if key not in seen:
            seen.add(key)
            parties.append({"name": name, "role": role})

    return parties


def parse_tentative_ruling(soup: BeautifulSoup) -> tuple[str | None, datetime | None]:
    """Extract tentative ruling text and hearing date from the ROA events table.

    Looks for rows with event type "Tentative Ruling" and extracts the ruling
    content div.

    Returns (ruling_text, hearing_date).
    """
    ruling_text: str | None = None
    hearing_date: datetime | None = None

    roa_table = soup.find("table", class_="roa-table")
    if not roa_table:
        return ruling_text, hearing_date

    tbody = roa_table.find("tbody")
    if not tbody:
        return ruling_text, hearing_date

    for row in tbody.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) < 3:
            continue

        event_type = tds[1].get_text(strip=True)
        if event_type.lower() != "tentative ruling":
            continue

        # Found a tentative ruling row
        ruling_div = tds[2].find("div", class_="ruling-content")
        if ruling_div:
            # Get text with newlines between paragraphs
            paragraphs = ruling_div.find_all("p")
            text_parts: list[str] = []
            for p in paragraphs:
                text = p.get_text(strip=True)
                if text and text != "\xa0":
                    text_parts.append(text)
            ruling_text = "\n\n".join(text_parts)

        # Extract hearing date from the ruling text
        desc_text = tds[2].get_text(" ", strip=True)
        m = _HEARING_DATE_RE.search(desc_text)
        if m:
            raw_date = " ".join(m.group("date").split())
            for fmt in ("%B %d, %Y", "%B %d %Y"):
                try:
                    hearing_date = datetime.strptime(raw_date, fmt)
                    break
                except ValueError:
                    continue

        # Take the first tentative ruling found
        break

    return ruling_text, hearing_date


def parse_outcome(text: str) -> str | None:
    """Extract the primary outcome from ruling text."""
    m = _OUTCOME_RE.search(text)
    if m:
        raw = m.group("outcome").strip()
        return " ".join(raw.upper().split())
    return None


def parse_motion_type(text: str) -> str | None:
    """Extract the motion type from ruling text."""
    m = _MOTION_TYPE_RE.search(text)
    if m:
        raw = m.group("motion_type").strip()
        normalized = " ".join(raw.split())
        if normalized and normalized[0].islower():
            normalized = normalized[0].upper() + normalized[1:]
        return normalized
    return None


def parse_roa_page(html: str) -> ROACaseInfo:
    """Parse a complete ROA case detail page into structured data.

    This is the main parsing entry point. It combines all the individual
    parsing functions to extract a complete ROACaseInfo.
    """
    soup = BeautifulSoup(html, "lxml")

    case_number, case_title = parse_case_header(soup)
    judge_name, department = parse_case_details(soup)
    parties = parse_parties(soup)
    ruling_text, hearing_date = parse_tentative_ruling(soup)

    outcome: str | None = None
    motion_type: str | None = None

    if ruling_text:
        outcome = parse_outcome(ruling_text)
        motion_type = parse_motion_type(ruling_text)

    return ROACaseInfo(
        case_number=case_number,
        case_title=case_title,
        judge_name=judge_name,
        department=department,
        hearing_date=hearing_date,
        ruling_text=ruling_text,
        outcome=outcome,
        motion_type=motion_type,
        parties=parties,
        has_tentative_ruling=ruling_text is not None,
    )


def parse_search_results(html: str) -> list[tuple[str, str]]:
    """Parse SmartSearch results page to extract case links.

    Returns list of (case_detail_url, case_number) tuples.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str]] = []

    for a_tag in soup.find_all("a", class_="case-link", href=True):
        href = a_tag["href"]
        case_number = a_tag.get_text(strip=True)
        if href and case_number:
            # Ensure absolute URL
            if href.startswith("/"):
                href = PORTAL_BASE_URL + href
            results.append((href, case_number))

    return results


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class SDTentativeRulingsScraper(BaseScraper):
    """San Diego County Odyssey ROA tentative rulings — Phase 2.

    Accepts case numbers (from the Phase 1 calendar scraper or directly)
    and retrieves tentative rulings from the Odyssey ROA portal using
    Playwright to solve Cloudflare challenges.

    Parameters
    ----------
    config : ScraperConfig
        Scraper configuration.
    case_numbers : list[str] | None
        Case numbers to look up. If None, no cases are fetched.
    proxy_url : str | None
        Optional residential proxy URL for Cloudflare bypass.
        Falls back to SD_PROXY_URL env var.
    headless : bool
        Whether to run the browser in headless mode. Default True.
    """

    def __init__(
        self,
        config: ScraperConfig,
        case_numbers: list[str] | None = None,
        proxy_url: str | None = None,
        headless: bool = True,
        archiver: S3Archiver | None = None,
        event_bus: EventBus | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, archiver=archiver, event_bus=event_bus)
        self._case_numbers = case_numbers or []
        self._proxy_url = proxy_url or os.environ.get("SD_PROXY_URL")
        self._headless = headless

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch tentative rulings for all case numbers via Playwright.

        Launches a browser, solves Cloudflare once, then iterates through
        case numbers searching for and extracting tentative rulings.
        """
        if not self._case_numbers:
            self._log.info("No case numbers provided, skipping ROA fetch")
            return []

        self._log.info(
            "Starting ROA fetch",
            case_count=len(self._case_numbers),
            proxy=bool(self._proxy_url),
        )

        return asyncio.run(self._fetch_all())

    async def _fetch_all(self) -> list[CapturedDocument]:
        """Async implementation of the fetch loop."""
        from playwright.async_api import async_playwright

        docs: list[CapturedDocument] = []

        async with async_playwright() as pw:
            launch_kwargs: dict[str, Any] = {
                "headless": self._headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--disable-sync",
                    "--no-first-run",
                    "--window-size=1920,1080",
                ],
            }

            if self._proxy_url:
                launch_kwargs["proxy"] = {"server": self._proxy_url}

            browser = await pw.chromium.launch(**launch_kwargs)

            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    java_script_enabled=True,
                    locale="en-US",
                    timezone_id="America/Los_Angeles",
                )

                page = await context.new_page()

                # Apply stealth evasions (fingerprint masking, webdriver
                # flag removal, etc.) to avoid Cloudflare bot detection.
                await _apply_stealth(page)

                # Step 1: Navigate to portal and solve Cloudflare challenge
                cf_solved = await self._solve_cloudflare(page)
                if not cf_solved:
                    self._log.error("Failed to solve Cloudflare challenge")
                    return docs

                # Step 2: For each case, search and extract ruling
                for i, case_number in enumerate(self._case_numbers):
                    if i > 0:
                        await asyncio.sleep(self.config.request_delay_seconds)

                    try:
                        doc = await self._fetch_case_ruling(page, case_number)
                        if doc is not None:
                            docs.append(doc)
                    except Exception as exc:
                        self._log.error(
                            "Failed to fetch case ruling",
                            case_number=case_number,
                            error=str(exc),
                        )

            finally:
                await browser.close()

        self._log.info(
            "ROA fetch complete",
            total_cases=len(self._case_numbers),
            rulings_found=len(docs),
        )

        return docs

    async def _solve_cloudflare(self, page: Any) -> bool:
        """Navigate to the portal and wait for Cloudflare challenge to resolve.

        Uses a retry loop with ``cf_clearance`` cookie polling to reliably
        detect when Cloudflare has been bypassed. The ``cf_clearance`` cookie
        is the definitive signal that the challenge is solved — it is set by
        Cloudflare's JavaScript after the challenge completes and is valid
        for approximately 30 minutes.

        Returns True if the challenge was solved (or no challenge was present),
        False if the challenge could not be solved after all retry attempts.
        """
        context = page.context

        for attempt in range(1, CF_MAX_RETRIES + 1):
            self._log.info(
                "Cloudflare solve attempt",
                attempt=attempt,
                max_retries=CF_MAX_RETRIES,
            )

            try:
                # Use wait_until="commit" so the goto returns as soon as
                # the server responds, rather than waiting for DOM parsing
                # which may time out on Cloudflare's challenge page.
                await page.goto(
                    f"{PORTAL_BASE_URL}/portal/",
                    timeout=PAGE_LOAD_TIMEOUT,
                    wait_until="commit",
                )

                # Check if we hit a Cloudflare challenge
                content = await page.content()
                if not is_cloudflare_challenge(content):
                    self._log.info("No Cloudflare challenge detected")
                    return True

                self._log.info("Cloudflare challenge detected, polling for cf_clearance cookie")

                # Poll for cf_clearance cookie — the definitive signal that
                # the challenge has been solved.  Cloudflare's JS executes
                # the challenge in the background and sets this cookie when
                # done.  Polling the cookie is more reliable than wait_for_url
                # because the URL does not always change after challenge
                # resolution.
                solved = await self._poll_cf_clearance(context)

                if solved:
                    self._log.info(
                        "Cloudflare challenge solved via cf_clearance cookie",
                        attempt=attempt,
                    )
                    return True

                # Cookie polling timed out — check if the page content
                # changed even without the cookie (some Cloudflare configs
                # don't set cf_clearance).
                content = await page.content()
                if not is_cloudflare_challenge(content):
                    self._log.info(
                        "Cloudflare challenge resolved (page content changed)",
                        attempt=attempt,
                    )
                    return True

                self._log.warning(
                    "Cloudflare challenge not resolved on this attempt",
                    attempt=attempt,
                    timeout_seconds=CF_CHALLENGE_TIMEOUT,
                )

            except Exception as exc:
                self._log.warning(
                    "Error during Cloudflare challenge attempt",
                    attempt=attempt,
                    error=str(exc),
                )

            # Brief pause before retrying to let Cloudflare state settle
            if attempt < CF_MAX_RETRIES:
                await asyncio.sleep(3.0)

        self._log.error(
            "Failed to solve Cloudflare challenge after all retries",
            max_retries=CF_MAX_RETRIES,
        )
        return False

    async def _poll_cf_clearance(self, context: Any) -> bool:
        """Poll for the cf_clearance cookie until timeout.

        Returns True if the cookie appears within ``CF_CHALLENGE_TIMEOUT``
        seconds, False otherwise.
        """
        elapsed = 0.0
        while elapsed < CF_CHALLENGE_TIMEOUT:
            if await has_cf_clearance(context):
                return True
            await asyncio.sleep(CF_POLL_INTERVAL)
            elapsed += CF_POLL_INTERVAL
        return False

    async def _fetch_case_ruling(self, page: Any, case_number: str) -> CapturedDocument | None:
        """Search for a case by number and extract the tentative ruling.

        Returns a CapturedDocument if a tentative ruling was found, None otherwise.
        """
        # Navigate to SmartSearch
        search_url = f"{SMART_SEARCH_URL}?searchString={case_number}"
        self._log.debug("Searching for case", case_number=case_number, url=search_url)

        await page.goto(search_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
        search_html = await page.content()

        # Check for Cloudflare re-challenge
        if is_cloudflare_challenge(search_html):
            self._log.warning("Cloudflare re-challenge during search", case_number=case_number)
            return None

        # Parse search results to find the case detail link
        results = parse_search_results(search_html)
        if not results:
            self._log.debug("No search results found", case_number=case_number)
            return None

        # Navigate to the first matching case detail page
        case_url, matched_number = results[0]
        self._log.debug(
            "Found case in search results",
            case_number=case_number,
            matched=matched_number,
            url=case_url,
        )

        await asyncio.sleep(1.0)  # Brief delay between search and detail page
        await page.goto(case_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
        detail_html = await page.content()

        # Check for Cloudflare re-challenge
        if is_cloudflare_challenge(detail_html):
            self._log.warning(
                "Cloudflare re-challenge on case detail",
                case_number=case_number,
            )
            return None

        # Parse the ROA page
        case_info = parse_roa_page(detail_html)

        if not case_info.has_tentative_ruling:
            self._log.debug(
                "No tentative ruling found in ROA",
                case_number=case_number,
            )
            return None

        # Create CapturedDocument
        doc = self._make_base_doc(
            source_url=case_url,
            raw_content=detail_html.encode("utf-8"),
            content_format=ContentFormat.HTML,
        )

        doc.case_number = case_info.case_number
        doc.case_title = case_info.case_title
        doc.judge_name = case_info.judge_name
        doc.department = case_info.department
        doc.hearing_date = case_info.hearing_date
        doc.ruling_text = case_info.ruling_text
        doc.outcome = case_info.outcome
        doc.motion_type = case_info.motion_type
        doc.parties = case_info.parties
        doc.extra["source_case_number"] = case_number
        doc.extra["portal_url"] = PORTAL_BASE_URL

        self._log.info(
            "Extracted tentative ruling",
            case_number=case_info.case_number,
            judge=case_info.judge_name,
            department=case_info.department,
            outcome=case_info.outcome,
        )

        return doc

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Re-parse a document from its raw HTML content.

        This is called by the base class run() loop after fetch_documents().
        Since we already parse during fetch, this serves as a re-parse
        opportunity that can enrich fields from the raw content.

        When ``ENABLE_SD_LLM_EXTRACTION`` is set, supplements outcome and
        motion_type via an LLM call using the validated San Diego prompt
        (#1967).  The LLM result takes priority over the regex fallback
        because it handles nuanced outcomes (mixed demurrer rulings, partial
        grants) more accurately.  Regex remains as a fallback when LLM is
        disabled or fails.
        """
        try:
            html = doc.raw_content.decode("utf-8")
            case_info = parse_roa_page(html)

            # Only overwrite fields that are currently None
            if case_info.case_number and not doc.case_number:
                doc.case_number = case_info.case_number
            if case_info.case_title and not doc.case_title:
                doc.case_title = case_info.case_title
            if case_info.judge_name and not doc.judge_name:
                doc.judge_name = case_info.judge_name
            if case_info.department and not doc.department:
                doc.department = case_info.department
            if case_info.hearing_date and not doc.hearing_date:
                doc.hearing_date = case_info.hearing_date
            if case_info.ruling_text and not doc.ruling_text:
                doc.ruling_text = case_info.ruling_text
            if case_info.parties and not doc.parties:
                doc.parties = case_info.parties

            # LLM extraction path (#2056): when enabled, extract outcome
            # and motion_type via the validated San Diego prompt.  The LLM
            # is more accurate for nuanced outcomes (mixed demurrer rulings,
            # partial grants) and more specific motion_type descriptions
            # than the regex keyword list.
            llm_used = False
            ruling_text = case_info.ruling_text or doc.ruling_text
            if _sd_llm_enabled() and ruling_text:
                llm_result = _sd_llm_extract(
                    ruling_text,
                    case_number=doc.case_number or case_info.case_number,
                )
                if llm_result is not None:
                    llm_used = True
                    doc.extra["_llm_extracted"] = True
                    if llm_result.get("outcome"):
                        doc.outcome = llm_result["outcome"]
                    if llm_result.get("motion_type"):
                        doc.motion_type = llm_result["motion_type"]

            # Regex fallback: fill in outcome and motion_type from the
            # HTML-parsed values when the LLM did not populate them
            # (or when LLM was disabled/failed).
            if case_info.outcome and not doc.outcome:
                doc.outcome = case_info.outcome
            if case_info.motion_type and not doc.motion_type:
                doc.motion_type = case_info.motion_type

            if not llm_used:
                self._log.debug(
                    "sd.regex_extraction",
                    case_number=doc.case_number,
                )

        except Exception as exc:
            self._log.warning("ROA re-parse error", error=str(exc))

        return doc


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default San Diego tentative rulings scraper configuration."""
    from datetime import time as dtime

    from framework import ScheduleWindow

    return ScraperConfig(
        scraper_id="ca-sd-tentatives",
        state="CA",
        county="San Diego",
        court="Superior Court",
        target_urls=[PORTAL_BASE_URL],
        poll_interval_seconds=86400,  # daily
        schedule_windows=[
            ScheduleWindow(start=dtime(16, 15), end=dtime(17, 15)),  # 4:15 PM primary
            ScheduleWindow(start=dtime(2, 0), end=dtime(3, 0)),  # 2 AM catch-up
        ],
        request_delay_seconds=DEFAULT_REQUEST_DELAY,
        request_timeout_seconds=30.0,
        max_retries=2,  # Fewer retries — browser restarts are expensive
        s3_bucket=s3_bucket,
    )
