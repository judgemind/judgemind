"""LA County Superior Court — Civil Tentative Rulings Scraper (Pattern 1).

Implements the dropdown-enumeration strategy documented in the LA Court
Scraping Investigation. Fetches every published civil tentative ruling
by enumerating all (courthouse, department, date) combinations from the
ASP.NET dropdown, then POSTing for each one.

URL: https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx?casetype=civil

Key implementation notes (from investigation):
- Server-rendered ASP.NET WebForms — simple HTTP only, no Playwright needed
- ViewState + EventValidation tokens required for each POST
- ~100 dropdown entries on a typical day
- No CAPTCHA on civil tentatives
- Each department formats rulings differently — parser is best-effort
- Recommended schedule: primary at 6 PM, catch-up at 2 AM
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

logger = structlog.get_logger(__name__)

BASE_URL = "https://www.lacourt.ca.gov/tentativeRulingNet/ui/main.aspx"
CIVIL_URL = f"{BASE_URL}?casetype=civil"

# Dropdown option format: "(Courthouse Name: Dept. XX) Month Day, Year"
# e.g. "(Stanley Mosk: Dept. 52) March 3, 2026"
_DROPDOWN_RE = re.compile(
    r"\((?P<courthouse>[^:]+):\s*Dept\.?\s*(?P<department>[^)]+)\)\s*(?P<date>.+)"
)

# LA County case number formats:
# New: 25STCV13276  (YYLLCC#####)
# Old: BC123456  (district+type+number)
_CASE_NUMBER_RE = re.compile(
    r"\b(?:\d{2}[A-Z]{2,4}\d{4,6}|[A-Z]{2}\d{5,7})\b"
)

# Common judge name patterns in ruling text
_JUDGE_RE = re.compile(
    r"(?:Judge|Hon\.?|Honorable)\s+([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)",
    re.IGNORECASE,
)


@dataclass
class DropdownOption:
    value: str          # the <option> value attribute (used in POST)
    courthouse: str
    department: str
    hearing_date: datetime | None
    raw_text: str


class LATentativeRulingsScraper(BaseScraper):
    """Scrapes all published LA County civil tentative rulings via dropdown enumeration.

    Usage:
        config = ScraperConfig(
            scraper_id="ca-la-tentatives-civil",
            state="CA",
            county="Los Angeles",
            court="Superior Court",
            target_urls=[CIVIL_URL],
            s3_bucket="judgemind-document-archive-dev",
        )
        scraper = LATentativeRulingsScraper(config=config, archiver=..., event_bus=...)
        health = scraper.run()
    """

    def fetch_documents(self) -> list[CapturedDocument]:
        """Enumerate dropdown, POST for each option, return raw HTML docs."""
        docs = []
        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
        ) as client:
            # Step 1: GET the page to obtain tokens and dropdown options
            self._log.info("Fetching main tentative rulings page", url=CIVIL_URL)
            response = client.get(CIVIL_URL)
            response.raise_for_status()
            main_html = response.text

            tokens = _extract_aspnet_tokens(main_html)
            options = _parse_dropdown_options(main_html)
            self._log.info("Found dropdown options", count=len(options))

            # Step 2: POST for each dropdown option
            for opt in options:
                time.sleep(self.config.request_delay_seconds)
                try:
                    ruling_html = _post_for_ruling(client, tokens, opt)
                    doc = self._make_base_doc(
                        source_url=CIVIL_URL,
                        raw_content=ruling_html.encode("utf-8"),
                        content_format=ContentFormat.HTML,
                    )
                    doc.courthouse = opt.courthouse
                    doc.department = opt.department
                    doc.hearing_date = opt.hearing_date
                    doc.extra["dropdown_value"] = opt.value
                    doc.extra["dropdown_raw"] = opt.raw_text
                    docs.append(doc)
                    self._log.debug(
                        "Fetched ruling",
                        courthouse=opt.courthouse,
                        dept=opt.department,
                        date=opt.hearing_date,
                    )
                except Exception as exc:
                    self._log.error(
                        "Failed to fetch ruling for option",
                        courthouse=opt.courthouse,
                        dept=opt.department,
                        error=str(exc),
                    )

        return docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Extract structured fields from the ruling HTML response."""
        try:
            soup = BeautifulSoup(doc.raw_content, "lxml")
            _extract_ruling_fields(soup, doc)
        except Exception as exc:
            self._log.warning("Parse error — returning partial doc", error=str(exc))
        return doc


# ---------------------------------------------------------------------------
# ASP.NET WebForms helpers
# ---------------------------------------------------------------------------


def _extract_aspnet_tokens(html: str) -> dict[str, str]:
    """Extract __VIEWSTATE, __VIEWSTATEGENERATOR, __EVENTVALIDATION from HTML."""
    soup = BeautifulSoup(html, "lxml")
    tokens: dict[str, str] = {}
    for field in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"):
        el = soup.find("input", {"name": field})
        if el:
            tokens[field] = el.get("value", "")
    if not tokens.get("__VIEWSTATE"):
        logger.warning("No __VIEWSTATE found — POST will likely fail")
    return tokens


def _parse_dropdown_options(html: str) -> list[DropdownOption]:
    """Parse all (courthouse, department, date) options from the dropdown."""
    soup = BeautifulSoup(html, "lxml")

    # The dropdown is a <select> — find by common name patterns
    select = (
        soup.find("select", {"name": re.compile(r"ddlHearingDate", re.I)})
        or soup.find("select", {"id": re.compile(r"ddlHearingDate", re.I)})
        or soup.find("select")  # fallback: first select
    )
    if not select:
        logger.warning("Dropdown not found in page HTML")
        return []

    options = []
    for opt_el in select.find_all("option"):
        value = opt_el.get("value", "").strip()
        text = opt_el.get_text(strip=True)
        if not value or value == "0":  # skip the placeholder option
            continue
        options.append(_parse_option_text(value, text))

    return [o for o in options if o is not None]


def _parse_option_text(value: str, text: str) -> DropdownOption | None:
    """Parse a dropdown option string into structured fields.

    Expected format: "(Courthouse Name: Dept. XX) Month Day, Year"
    e.g.: "(Stanley Mosk: Dept. 52) March 3, 2026"
    """
    m = _DROPDOWN_RE.match(text)
    if not m:
        logger.debug("Could not parse dropdown option", text=text)
        return DropdownOption(
            value=value,
            courthouse="Unknown",
            department="Unknown",
            hearing_date=None,
            raw_text=text,
        )

    courthouse = m.group("courthouse").strip()
    department = m.group("department").strip()
    date_str = m.group("date").strip()

    hearing_date: datetime | None = None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            hearing_date = datetime.strptime(date_str, fmt)
            break
        except ValueError:
            continue

    return DropdownOption(
        value=value,
        courthouse=courthouse,
        department=department,
        hearing_date=hearing_date,
        raw_text=text,
    )


def _post_for_ruling(
    client: httpx.Client,
    tokens: dict[str, str],
    option: DropdownOption,
) -> str:
    """Submit the ASP.NET form for a single dropdown option and return the response HTML."""
    form_data = {
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": tokens.get("__VIEWSTATE", ""),
        "__VIEWSTATEGENERATOR": tokens.get("__VIEWSTATEGENERATOR", ""),
        "__EVENTVALIDATION": tokens.get("__EVENTVALIDATION", ""),
        "ddlHearingDate": option.value,
        "btnSearch": "Search",  # the submit button
    }
    response = client.post(CIVIL_URL, data=form_data)
    response.raise_for_status()
    return response.text


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------


def _extract_ruling_fields(soup: BeautifulSoup, doc: CapturedDocument) -> None:
    """Populate structured fields on doc from the ruling response HTML.

    The page structure varies by department. We use heuristics that work
    across most departments, and store unparseable content in ruling_text
    for downstream NLP processing.
    """
    # Full text for NLP fallback
    full_text = soup.get_text(separator="\n", strip=True)
    doc.ruling_text = full_text

    # Case numbers — find all matches and store the first as primary
    case_numbers = _CASE_NUMBER_RE.findall(full_text)
    if case_numbers:
        doc.case_number = case_numbers[0]
        if len(case_numbers) > 1:
            doc.extra["additional_case_numbers"] = case_numbers[1:]

    # Judge name
    judge_match = _JUDGE_RE.search(full_text)
    if judge_match:
        doc.judge_name = judge_match.group(1).strip()

    # Department and courthouse are already set from the dropdown option
    # (passed in by fetch_documents via doc.extra)


# ---------------------------------------------------------------------------
# Default config factory
# ---------------------------------------------------------------------------


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Return a ready-to-use ScraperConfig for the LA civil tentatives scraper."""
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
            ScheduleWindow(start=dtime(18, 0), end=dtime(19, 0)),   # 6 PM sweep
            ScheduleWindow(start=dtime(2, 0), end=dtime(3, 0)),     # 2 AM catch-up
        ],
        request_delay_seconds=1.5,    # be a good citizen
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
