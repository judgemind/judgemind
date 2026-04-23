"""Governor's judicial appointment press release scraper.

Scrapes the California Governor's website for judicial appointment press
releases and extracts per-judge biographical data (name, county, court,
position, education) from the structured text.

Source: https://www.gov.ca.gov/?s=judicial+appointment
Individual press release URL pattern:
  https://www.gov.ca.gov/YYYY/MM/DD/governor-newsom-announces-judicial-appointments-...

Each press release contains one paragraph per appointee following a consistent
template that includes the judge's name, county, court, current/previous
positions, and education.

Issue: #2165
Parent: #2144
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx
import structlog
from bs4 import BeautifulSoup, Tag

from framework import BaseScraper, CapturedDocument, ContentFormat, ScheduleWindow, ScraperConfig

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEARCH_URL = "https://www.gov.ca.gov/"
SEARCH_QUERY = "judicial+appointment"

# Default User-Agent for polite scraping.
USER_AGENT = "Judgemind/1.0 (+https://judgemind.org/scraper)"

# Maximum search result pages to paginate through.
DEFAULT_MAX_PAGES = 10

# ---------------------------------------------------------------------------
# Appointee parsing
# ---------------------------------------------------------------------------

# Matches the opening of a typical appointment paragraph:
#   "Full Name, of County, has been appointed to serve as a Judge in the
#    County Superior Court."
# Group captures: name, county, court_county (often same as county).
_APPT_PATTERN = re.compile(
    r"(?P<name>[A-Z][A-Za-z\s\.\-']+?),\s+of\s+(?P<county>[A-Za-z\s]+?),\s+"
    r"has\s+been\s+appointed\s+to\s+serve\s+as\s+(?:a\s+)?(?P<role>[A-Za-z\s]+?)\s+"
    r"(?:in|on|of)\s+(?:the\s+)?(?P<court>[A-Za-z\s]+?(?:Superior\s+Court|Court\s+of\s+Appeal"
    r"|Supreme\s+Court)[A-Za-z\s,]*?)(?:\.|,|;|$)",
    re.DOTALL,
)

# Education extraction uses a two-step approach.
#
# Step 1: Isolate the full education clause beginning at "earned".
# This captures the entire run of "earned a JD from X, and a BA from Y."
_EDUCATION_CLAUSE_RE = re.compile(
    r"earned\s+(?P<clause>.+?)(?:\.(?:\s|$)|$)",
    re.IGNORECASE | re.DOTALL,
)

# Step 2: Extract individual (degree, institution) pairs from the clause.
# This regex does NOT require an "earned" anchor, so finditer can match
# the second, third, etc. degrees that are linked with "and a".
_EDUCATION_PAIR_RE = re.compile(
    r"\s*(?:a\s+|an\s+)?(?P<degree>[A-Za-z\s\.,']+?)\s+"
    r"(?:degree\s+)?from\s+(?P<institution>[A-Za-z\s\.,'&]+?)(?:,\s+and\b|\s+and\s+a\b|,|$)",
    re.IGNORECASE | re.DOTALL,
)

# Position pattern — "has served as {role} at/for/in {employer} since {year}"
_POSITION_RE = re.compile(
    r"has\s+served\s+as\s+(?:a\s+|an\s+)?(?P<role>[A-Za-z\s\-]+?)\s+"
    r"(?:at|for|in|with)\s+(?P<employer>[A-Za-z\s\.\-,'&]+?)\s+"
    r"since\s+(?P<since>\d{4})",
    re.IGNORECASE | re.DOTALL,
)

# Broader position pattern — "served as {role} at/in {employer}"
_PREV_POSITION_RE = re.compile(
    r"(?:previously\s+)?served\s+as\s+(?:a\s+|an\s+)?(?P<role>[A-Za-z\s\-]+?)\s+"
    r"(?:at|for|in|with)\s+(?P<employer>[A-Za-z\s\.\-,'&]+?)(?:\s+from\s+"
    r"(?P<from_year>\d{4})\s+to\s+(?P<to_year>\d{4}))?(?:\.|,|;|$)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class Appointee:
    """Parsed data for a single judicial appointee from a press release."""

    name: str
    county: str
    court: str
    role: str = "Judge"
    current_position: str | None = None
    employer: str | None = None
    since_year: int | None = None
    education: list[dict[str, str]] = field(default_factory=list)
    previous_positions: list[dict[str, str]] = field(default_factory=list)
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict for storage in CapturedDocument.extra."""
        result: dict[str, Any] = {
            "name": self.name,
            "county": self.county,
            "court": self.court,
            "role": self.role,
        }
        if self.current_position:
            result["current_position"] = self.current_position
        if self.employer:
            result["employer"] = self.employer
        if self.since_year:
            result["since_year"] = self.since_year
        if self.education:
            result["education"] = self.education
        if self.previous_positions:
            result["previous_positions"] = self.previous_positions
        result["raw_text"] = self.raw_text
        return result


def parse_appointees(html: str) -> list[Appointee]:
    """Parse individual appointee entries from a press release HTML page.

    Each appointee is described in one or more paragraphs. We identify them
    by the "{Name}, of {County}, has been appointed..." pattern and extract
    biographical data from the surrounding text.

    The parser is stateful: it accumulates consecutive paragraphs after an
    appointment match until the next appointment pattern or a delimiter
    (such as ``###``) is encountered. This handles press releases where
    biographical details are split across multiple ``<p>`` tags.
    """
    soup = BeautifulSoup(html, "lxml")

    # The press release content is typically in <p> tags within the main
    # article body. We search all paragraphs for appointment patterns.
    paragraphs = soup.find_all("p")

    # Phase 1: Group paragraph texts into per-appointee text blocks.
    # Each block starts with a paragraph matching _APPT_PATTERN and
    # accumulates subsequent paragraphs until the next appointment
    # pattern or a delimiter like "###".
    text_blocks: list[str] = []
    current_block: list[str] = []

    for p in paragraphs:
        text = p.get_text(separator=" ", strip=True)
        if not text:
            continue

        # Delimiter: "###" marks the end of the press release body.
        if text.strip().startswith("###"):
            if current_block:
                text_blocks.append(" ".join(current_block))
                current_block = []
            continue

        if _APPT_PATTERN.search(text):
            # New appointee detected — flush the previous block.
            if current_block:
                text_blocks.append(" ".join(current_block))
            current_block = [text]
        elif current_block:
            # Continuation paragraph for the current appointee.
            current_block.append(text)
        # else: ignore paragraphs before the first appointee (e.g. "SACRAMENTO - ...")

    # Flush the final block.
    if current_block:
        text_blocks.append(" ".join(current_block))

    # Phase 2: Extract appointee data from each accumulated text block.
    appointees: list[Appointee] = []

    for block_text in text_blocks:
        match = _APPT_PATTERN.search(block_text)
        if not match:
            continue

        name = _clean_whitespace(match.group("name"))
        county = _clean_whitespace(match.group("county"))
        court = _clean_whitespace(match.group("court"))
        role = _clean_whitespace(match.group("role"))

        appointee = Appointee(
            name=name,
            county=county,
            court=court,
            role=role,
            raw_text=block_text,
        )

        # Extract current position
        pos_match = _POSITION_RE.search(block_text)
        if pos_match:
            appointee.current_position = _clean_whitespace(pos_match.group("role"))
            appointee.employer = _clean_whitespace(pos_match.group("employer"))
            appointee.since_year = int(pos_match.group("since"))

        # Extract education — two-step: isolate clause, then extract pairs
        clause_match = _EDUCATION_CLAUSE_RE.search(block_text)
        if clause_match:
            edu_clause = clause_match.group("clause")
            for edu_match in _EDUCATION_PAIR_RE.finditer(edu_clause):
                appointee.education.append(
                    {
                        "degree": _clean_whitespace(edu_match.group("degree")),
                        "institution": _clean_whitespace(edu_match.group("institution")),
                    }
                )

        # Extract previous positions
        for prev_match in _PREV_POSITION_RE.finditer(block_text):
            # Skip if this is the same as current position
            prev_role = _clean_whitespace(prev_match.group("role"))
            prev_employer = _clean_whitespace(prev_match.group("employer"))
            if prev_role == appointee.current_position and prev_employer == appointee.employer:
                continue
            pos: dict[str, str] = {
                "role": prev_role,
                "employer": prev_employer,
            }
            if prev_match.group("from_year"):
                pos["from_year"] = prev_match.group("from_year")
            if prev_match.group("to_year"):
                pos["to_year"] = prev_match.group("to_year")
            appointee.previous_positions.append(pos)

        appointees.append(appointee)

    return appointees


# ---------------------------------------------------------------------------
# Search result parsing
# ---------------------------------------------------------------------------


def extract_search_results(html: str, base_url: str) -> list[dict[str, str]]:
    """Extract press release links from the gov.ca.gov search results page.

    Returns a list of dicts with keys: url, title, date (if available).
    Only includes results that look like judicial appointment press releases.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    # Search results are typically in <article> or <h2> > <a> patterns
    # in the WordPress theme used by gov.ca.gov.
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        text = link.get_text(strip=True)

        if not text:
            continue

        # Resolve relative URLs
        if not href.startswith("http"):
            href = urljoin(base_url, href)

        # Deduplicate
        if href in seen:
            continue

        # Filter: must be a gov.ca.gov press release about judicial appointments
        if not _is_appointment_url(href, text):
            continue

        seen.add(href)
        result: dict[str, str] = {"url": href, "title": text}

        # Try to extract date from URL path (YYYY/MM/DD pattern)
        date_match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", href)
        if date_match:
            result["date"] = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"

        results.append(result)

    return results


def extract_next_page_url(html: str, base_url: str) -> str | None:
    """Extract the next page URL from search results pagination.

    Returns the absolute URL of the next page, or None if there is no next page.
    """
    soup = BeautifulSoup(html, "lxml")

    # WordPress search pagination typically uses <a class="next"> or
    # <a> with "next" or "Next" text, or rel="next".
    next_link = soup.find("a", class_="next")
    if next_link and isinstance(next_link, Tag) and next_link.get("href"):
        return urljoin(base_url, str(next_link["href"]))

    # Also check for rel="next"
    next_link = soup.find("a", rel="next")
    if next_link and isinstance(next_link, Tag) and next_link.get("href"):
        return urljoin(base_url, str(next_link["href"]))

    # Check for "Next" or "next" text in pagination links
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if text in ("next", "next page", "older entries"):
            return urljoin(base_url, str(a["href"]))

    return None


def _is_appointment_url(url: str, title: str) -> bool:
    """Check if a URL and title indicate a judicial appointment press release.

    Filters out pagination/search URLs (which contain the search query in
    their query string but are not actual press releases).
    """
    url_lower = url.lower()
    title_lower = title.lower()

    # URL-based checks
    if "gov.ca.gov" not in url_lower:
        return False

    # Exclude pagination and search result URLs — they contain the query
    # string but are not actual press release pages.
    if "?s=" in url_lower or "/page/" in url_lower:
        return False

    # Must contain judicial/appointment keywords in URL or title
    url_keywords = ("judicial-appointment", "appoints-judge")
    title_keywords = (
        "judicial appointment",
        "announces judicial",
        "appoints judge",
        "judicial nominations",
    )

    url_match = any(kw in url_lower for kw in url_keywords)
    title_match = any(kw in title_lower for kw in title_keywords)

    return url_match or title_match


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class GovernorAppointmentsScraper(BaseScraper):
    """Scrapes Governor's judicial appointment press releases for judge bios.

    Strategy:
    1. Search gov.ca.gov for "judicial appointment" press releases
    2. Paginate through search results to discover all press release URLs
    3. Fetch each press release page
    4. Parse per-judge biographical data from the structured text
    5. Store one CapturedDocument per press release with appointees in extra
    """

    def __init__(
        self,
        config: ScraperConfig,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        self._max_pages = max_pages

    def fetch_documents(self) -> list[CapturedDocument]:
        """Discover and fetch all judicial appointment press releases."""
        docs: list[CapturedDocument] = []

        with httpx.Client(
            timeout=self.config.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            # Phase 1: Discover press release URLs from search
            press_release_urls = self._discover_press_releases(client)
            self._log.info("Discovered press releases", count=len(press_release_urls))

            # Phase 2: Fetch each press release
            for pr_info in press_release_urls:
                time.sleep(self.config.request_delay_seconds)
                try:
                    doc = self._fetch_press_release(client, pr_info)
                    docs.append(doc)
                    self._log.debug(
                        "Fetched press release",
                        url=pr_info["url"],
                        title=pr_info.get("title", ""),
                    )
                except Exception as exc:
                    self._log.error(
                        "Failed to fetch press release",
                        url=pr_info["url"],
                        error=str(exc),
                    )

        return docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Parse per-judge biographical data from the press release HTML."""
        try:
            html = doc.raw_content.decode("utf-8", errors="replace")
            appointees = parse_appointees(html)

            # Store appointee data in extra
            doc.extra["appointees"] = [a.to_dict() for a in appointees]
            doc.extra["appointee_count"] = len(appointees)

            # Set ruling_text to the combined biographical text for downstream use
            if appointees:
                doc.ruling_text = "\n\n".join(a.raw_text for a in appointees)

            # Use the first appointee's name as a representative for the document
            if appointees:
                doc.judge_name = appointees[0].name
                doc.extra["all_judge_names"] = [a.name for a in appointees]

        except Exception as exc:
            self._log.warning(
                "Press release parse error",
                source_url=doc.source_url,
                error=str(exc),
            )

        return doc

    def _discover_press_releases(self, client: httpx.Client) -> list[dict[str, str]]:
        """Search gov.ca.gov and paginate to find all appointment press release URLs."""
        all_results: list[dict[str, str]] = []
        seen_urls: set[str] = set()

        # Start with the search URL
        search_url = f"{SEARCH_URL}?s={SEARCH_QUERY}"
        pages_fetched = 0

        while search_url and pages_fetched < self._max_pages:
            self._log.info("Fetching search page", url=search_url, page=pages_fetched + 1)

            if pages_fetched > 0:
                time.sleep(self.config.request_delay_seconds)

            response = client.get(search_url)
            response.raise_for_status()

            results = extract_search_results(response.text, search_url)
            for r in results:
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    all_results.append(r)

            search_url_next = extract_next_page_url(response.text, search_url)
            search_url = search_url_next  # type: ignore[assignment]
            pages_fetched += 1

            self._log.debug(
                "Search page results",
                page=pages_fetched,
                new_results=len(results),
                total=len(all_results),
            )

        return all_results

    def _fetch_press_release(
        self, client: httpx.Client, pr_info: dict[str, str]
    ) -> CapturedDocument:
        """Fetch a single press release page and create a CapturedDocument."""
        response = client.get(pr_info["url"])
        response.raise_for_status()

        doc = self._make_base_doc(
            source_url=pr_info["url"],
            raw_content=response.content,
            content_format=ContentFormat.HTML,
        )

        # Store metadata from the search result
        doc.extra["press_release_title"] = pr_info.get("title", "")
        if "date" in pr_info:
            doc.extra["press_release_date"] = pr_info["date"]
            # Also set hearing_date from the press release date
            try:
                doc.hearing_date = datetime.strptime(pr_info["date"], "%Y-%m-%d").replace(
                    tzinfo=UTC
                )
            except ValueError:
                pass

        doc.extra["source_type"] = "governor_appointment"

        return doc


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default Governor appointments scraper configuration."""
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="ca-governor-appointments",
        state="CA",
        county="Statewide",
        court="Governor",
        target_urls=[f"{SEARCH_URL}?s={SEARCH_QUERY}"],
        poll_interval_seconds=604800,  # weekly
        schedule_windows=[
            ScheduleWindow(
                start=dtime(2, 0),
                end=dtime(4, 0),
                timezone="America/Los_Angeles",
            ),
        ],
        request_delay_seconds=2.0,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_whitespace(text: str) -> str:
    """Normalize whitespace in a string."""
    return " ".join(text.split()).strip()
