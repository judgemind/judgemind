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

# Judge name: "<div>William A. Crowfoot Judge of the Superior Court</div>"
_JUDGE_DIV_RE = re.compile(r"(.+?)\s+Judge of the Superior Court", re.DOTALL)


@dataclass
class DropdownOption:
    value: str  # raw option value, used in POST
    courthouse_code: str  # e.g. "ALH"
    courthouse: str  # e.g. "Alhambra Courthouse"
    department: str  # e.g. "3"
    hearing_date: datetime | None


class LATentativeRulingsScraper(BaseScraper):
    """Scrapes all published LA County civil tentative rulings via dropdown enumeration."""

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
                    docs.append(doc)
                    self._log.debug(
                        "Fetched ruling",
                        courthouse=opt.courthouse,
                        dept=opt.department,
                        date=str(opt.hearing_date),
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


def _extract_ruling_fields(soup: BeautifulSoup, doc: CapturedDocument) -> None:
    """Extract structured fields from the ruling response HTML.

    The ruling content lives in div#speechSynthesis.
    A single response may contain rulings for multiple cases.
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

    # Judge name from the signature div
    for div in content.find_all("div"):
        div_text = div.get_text(separator=" ", strip=True)
        m = _JUDGE_DIV_RE.match(div_text)
        if m:
            # Normalize whitespace in name
            doc.judge_name = " ".join(m.group(1).split())
            break


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
