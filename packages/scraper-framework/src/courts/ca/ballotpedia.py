"""Ballotpedia scraper for California trial court judge biographical data.

Fetches and parses Ballotpedia pages for judges to extract education, career
history, and election data. Ballotpedia covers approximately 67% of CA trial
court judges, biased toward those who have faced elections.

Trial court judge pages do NOT have infoboxes (unlike appellate/federal judges).
Data is extracted from structured MediaWiki HTML sections (Education, Career,
Elections) using heading-based parsing.

URL patterns vary by name format:
    - ``https://ballotpedia.org/Maria_T._Rodriguez``
    - ``https://ballotpedia.org/Maria_Rodriguez``

This module provides:
    - ``build_candidate_urls()`` — generate candidate Ballotpedia URLs for a judge name
    - ``parse_ballotpedia_page()`` — parse a Ballotpedia page into structured bio data
    - ``fetch_judge_bio()`` — fetch and parse a judge's Ballotpedia page
    - ``BallotpediaBioEntry`` — structured representation of extracted bio data

See: https://github.com/judgemind/judgemind/issues/2166
Parent: https://github.com/judgemind/judgemind/issues/2144
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import structlog
from botocore.exceptions import BotoCoreError, ClientError
from bs4 import BeautifulSoup, Tag

from framework.hashing import sha256_hex
from framework.s3_cache import make_s3_client

logger = structlog.get_logger(__name__)

BASE_URL = "https://ballotpedia.org"

# User-Agent identifies us as a good-faith scraper
USER_AGENT = "Judgemind/1.0 (+https://judgemind.org/scraper)"

# Default delay between requests (seconds) to be polite
DEFAULT_REQUEST_DELAY: float = 1.5


@dataclass
class EducationEntry:
    """A single education record from a Ballotpedia page."""

    institution: str
    degree: str | None = None
    year: str | None = None


@dataclass
class CareerEntry:
    """A single career history record from a Ballotpedia page."""

    position: str
    employer: str | None = None
    years: str | None = None


@dataclass
class ElectionEntry:
    """A single election record from a Ballotpedia page."""

    year: str
    race: str | None = None
    candidates: list[dict[str, str]] = field(default_factory=list)


@dataclass
class BallotpediaBioEntry:
    """Structured biographical data extracted from a Ballotpedia page.

    This maps to the ``judge_bio_sources`` jsonb format described in
    the parent epic (#2144):

    .. code-block:: json

        {
            "source": "ballotpedia",
            "url": "https://ballotpedia.org/Maria_T._Rodriguez",
            "fetched_at": "2026-03-28T12:00:00Z",
            "content": { ... structured sections ... }
        }
    """

    source_url: str
    fetched_at: datetime
    biography_text: str | None = None
    education: list[EducationEntry] = field(default_factory=list)
    career: list[CareerEntry] = field(default_factory=list)
    elections: list[ElectionEntry] = field(default_factory=list)
    raw_html: str | None = None
    s3_key: str | None = None

    def to_bio_source_dict(self) -> dict[str, object]:
        """Convert to the ``judge_bio_sources`` jsonb format.

        Returns a dict matching the schema from the parent epic (#2144)::

            {
                "source": "ballotpedia",
                "url": "...",
                "fetched_at": "...",
                "content": {
                    "biography_text": "...",
                    "education": [...],
                    "career": [...],
                    "elections": [...]
                }
            }
        """
        result: dict[str, object] = {
            "source": "ballotpedia",
            "url": self.source_url,
            "fetched_at": self.fetched_at.isoformat(),
            "content": {
                "biography_text": self.biography_text,
                "education": [
                    {
                        "institution": e.institution,
                        "degree": e.degree,
                        "year": e.year,
                    }
                    for e in self.education
                ],
                "career": [
                    {
                        "position": c.position,
                        "employer": c.employer,
                        "years": c.years,
                    }
                    for c in self.career
                ],
                "elections": [
                    {
                        "year": el.year,
                        "race": el.race,
                        "candidates": el.candidates,
                    }
                    for el in self.elections
                ],
            },
        }
        if self.s3_key is not None:
            result["s3_key"] = self.s3_key
        return result


def build_candidate_urls(
    first_name: str,
    last_name: str,
    middle_initial: str | None = None,
) -> list[str]:
    """Generate candidate Ballotpedia URLs for a judge name.

    Ballotpedia URL patterns for California trial court judges vary:
    - ``/First_M._Last`` (with middle initial and period)
    - ``/First_Last`` (without middle initial)

    Both patterns are tried in order. The first URL that returns a 200
    response wins.

    Parameters
    ----------
    first_name : str
        The judge's first name (e.g., "Maria").
    last_name : str
        The judge's last name (e.g., "Rodriguez").
    middle_initial : str | None
        The judge's middle initial (e.g., "T" or "T."). The period
        is added automatically if missing.

    Returns
    -------
    list[str]
        Candidate URLs to try, ordered by specificity (with middle
        initial first, then without).
    """
    urls: list[str] = []

    # Normalize name components: strip whitespace, replace spaces with underscores
    first = first_name.strip().replace(" ", "_")
    last = last_name.strip().replace(" ", "_")

    if middle_initial:
        mid = middle_initial.strip().rstrip(".")
        if mid:
            # Format: First_M._Last
            urls.append(f"{BASE_URL}/{first}_{mid}._{last}")

    # Format: First_Last (always tried as fallback)
    urls.append(f"{BASE_URL}/{first}_{last}")

    return urls


def _extract_section_content(
    soup: BeautifulSoup,
    section_id: str,
) -> Tag | None:
    """Extract the content between a section heading and the next heading.

    Ballotpedia uses MediaWiki's heading structure:
    ``<h2><span class="mw-headline" id="Education">Education</span></h2>``

    Content between headings includes ``<ul>``, ``<p>``, ``<table>``, etc.

    Parameters
    ----------
    soup : BeautifulSoup
        The parsed page.
    section_id : str
        The ``id`` attribute of the ``<span class="mw-headline">`` element.

    Returns
    -------
    Tag | None
        A new ``<div>`` tag containing all sibling elements between the
        section heading and the next h2 heading, or None if the section
        is not found.
    """
    headline = soup.find("span", {"class": "mw-headline", "id": section_id})
    if headline is None:
        return None

    # The heading is the parent <h2> or <h3> of the <span>
    heading_tag = headline.parent
    if heading_tag is None:
        return None

    # Collect all siblings until the next h2
    content = soup.new_tag("div")
    sibling = heading_tag.next_sibling
    while sibling is not None:
        if isinstance(sibling, Tag) and sibling.name in ("h2",):
            break
        if isinstance(sibling, Tag):
            content.append(sibling.__copy__())
        sibling = sibling.next_sibling

    return content


def _parse_education(section: Tag) -> list[EducationEntry]:
    """Parse education entries from a Ballotpedia Education section.

    Typical format in ``<li>`` elements:
    - ``B.A., University of California, Berkeley, 2005``
    - ``J.D., Stanford Law School, 2008``
    - ``B.S., University of Southern California, 2001``

    The parser handles variations:
    - Degree may be absent (just institution name)
    - Year may be absent
    - Institution may contain commas (e.g., "University of California, Berkeley")

    Parameters
    ----------
    section : Tag
        The parsed content of the Education section.

    Returns
    -------
    list[EducationEntry]
        Parsed education entries.
    """
    entries: list[EducationEntry] = []
    for li in section.find_all("li"):
        text = li.get_text(strip=True)
        if not text:
            continue

        entry = _parse_single_education_entry(text)
        if entry is not None:
            entries.append(entry)

    return entries


# Regex to detect a trailing 4-digit year (possibly preceded by a comma)
_YEAR_PATTERN = re.compile(r",?\s*(\d{4})\s*$")

# Common degree abbreviations
_DEGREE_PREFIXES = (
    "B.A.",
    "B.S.",
    "B.B.A.",
    "B.F.A.",
    "M.A.",
    "M.S.",
    "M.B.A.",
    "M.F.A.",
    "M.P.A.",
    "M.P.P.",
    "J.D.",
    "LL.M.",
    "LL.B.",
    "Ph.D.",
    "Ed.D.",
    "D.B.A.",
    "A.B.",
    "A.A.",
    "A.S.",
)


def _parse_single_education_entry(text: str) -> EducationEntry | None:
    """Parse a single education line into an EducationEntry.

    Expected formats:
    - ``"B.A., University of California, Berkeley, 2005"``
    - ``"J.D., Stanford Law School, 2008"``
    - ``"University of Southern California"`` (no degree)
    - ``"B.A., UCLA"`` (no year)

    Parameters
    ----------
    text : str
        The raw text of a single education list item.

    Returns
    -------
    EducationEntry | None
        Parsed entry, or None if the text is empty.
    """
    if not text.strip():
        return None

    # Extract year if present at end
    year: str | None = None
    year_match = _YEAR_PATTERN.search(text)
    if year_match:
        year = year_match.group(1)
        text = text[: year_match.start()].strip()

    # Extract degree if the text starts with a known degree prefix
    degree: str | None = None
    for prefix in _DEGREE_PREFIXES:
        if text.startswith(prefix):
            degree = prefix
            text = text[len(prefix) :].strip().lstrip(",").strip()
            break

    institution = text.strip()

    return EducationEntry(
        institution=institution,
        degree=degree,
        year=year,
    )


def _parse_career(section: Tag) -> list[CareerEntry]:
    """Parse career entries from a Ballotpedia Career section.

    Typical format in ``<li>`` elements:
    - ``Associate, Morrison & Foerster LLP, 2008-2012``
    - ``Deputy District Attorney, Los Angeles County District Attorney's Office, 2012-2018``

    Parameters
    ----------
    section : Tag
        The parsed content of the Career section.

    Returns
    -------
    list[CareerEntry]
        Parsed career entries.
    """
    entries: list[CareerEntry] = []
    for li in section.find_all("li"):
        text = li.get_text(strip=True)
        if not text:
            continue

        entry = _parse_single_career_entry(text)
        if entry is not None:
            entries.append(entry)

    return entries


# Pattern to match year ranges at the end: "2008-2012", "2018-present", "2022"
_CAREER_YEARS_PATTERN = re.compile(r",?\s*(\d{4}(?:\s*[-\u2013]\s*(?:\d{4}|[Pp]resent))?)\s*$")


def _parse_single_career_entry(text: str) -> CareerEntry | None:
    """Parse a single career line into a CareerEntry.

    Expected format: ``"Position, Employer, Years"``

    The first comma separates the position from the employer+years.
    Years (if present) are extracted from the end.

    Parameters
    ----------
    text : str
        The raw text of a single career list item.

    Returns
    -------
    CareerEntry | None
        Parsed entry, or None if the text is empty.
    """
    if not text.strip():
        return None

    # Extract years if present at end
    years: str | None = None
    years_match = _CAREER_YEARS_PATTERN.search(text)
    if years_match:
        years = years_match.group(1)
        text = text[: years_match.start()].strip()

    # Split on first comma to separate position from employer
    if "," in text:
        position, employer = text.split(",", 1)
        position = position.strip()
        employer = employer.strip()
    else:
        position = text.strip()
        employer = None

    return CareerEntry(
        position=position,
        employer=employer,
        years=years,
    )


def _parse_elections(section: Tag) -> list[ElectionEntry]:
    """Parse election entries from a Ballotpedia Elections section.

    Elections sections contain year subheadings (h3) with election details
    in tables or descriptive text.

    Parameters
    ----------
    section : Tag
        The parsed content of the Elections section.

    Returns
    -------
    list[ElectionEntry]
        Parsed election entries.
    """
    entries: list[ElectionEntry] = []

    # Find year subheadings (h3 elements)
    year_headings = section.find_all("h3")
    for heading in year_headings:
        headline_span = heading.find("span", class_="mw-headline")
        if headline_span is None:
            continue
        year_text = headline_span.get_text(strip=True)
        if not year_text.isdigit():
            continue

        # Search siblings for election tables — one entry per table
        sibling = heading.next_sibling
        while sibling is not None:
            if isinstance(sibling, Tag):
                if sibling.name in ("h3", "h2"):
                    break
                if sibling.name == "table":
                    table_candidates: list[dict[str, str]] = []
                    table_race: str | None = None
                    # Extract race name from caption
                    caption = sibling.find("caption")
                    if caption:
                        table_race = caption.get_text(strip=True)
                    # Extract candidates from table rows
                    for row in sibling.find_all("tr"):
                        cells = row.find_all("td")
                        if len(cells) >= 2:
                            candidate_name = cells[0].get_text(strip=True)
                            votes = cells[1].get_text(strip=True)
                            pct = cells[2].get_text(strip=True) if len(cells) >= 3 else ""
                            table_candidates.append(
                                {
                                    "name": candidate_name,
                                    "votes": votes,
                                    "percent": pct,
                                }
                            )
                    if table_candidates:
                        entries.append(
                            ElectionEntry(
                                year=year_text,
                                race=table_race,
                                candidates=table_candidates,
                            )
                        )
            sibling = sibling.next_sibling

    return entries


def _extract_biography_text(soup: BeautifulSoup) -> str | None:
    """Extract the introductory biography text from a Ballotpedia page.

    The biography is the paragraph text that appears before the table of
    contents (``<div id="toc">``), within the ``mw-parser-output`` div.

    Parameters
    ----------
    soup : BeautifulSoup
        The parsed page.

    Returns
    -------
    str | None
        The biography text, or None if not found.
    """
    parser_output = soup.find("div", class_="mw-parser-output")
    if parser_output is None:
        return None

    paragraphs: list[str] = []
    for child in parser_output.children:
        if isinstance(child, Tag):
            # Stop at table of contents or first heading
            if child.get("id") == "toc" or child.name in ("h2", "h3"):
                break
            if child.name == "p":
                text = child.get_text(strip=True)
                if text:
                    paragraphs.append(text)

    return " ".join(paragraphs) if paragraphs else None


def parse_ballotpedia_page(
    html: str,
    source_url: str,
    fetched_at: datetime | None = None,
) -> BallotpediaBioEntry:
    """Parse a Ballotpedia page into structured biographical data.

    Extracts Education, Career, and Elections sections from the MediaWiki
    HTML, plus the introductory biography text.

    Parameters
    ----------
    html : str
        The raw HTML of the Ballotpedia page.
    source_url : str
        The URL the page was fetched from.
    fetched_at : datetime | None
        When the page was fetched. Defaults to now (UTC).

    Returns
    -------
    BallotpediaBioEntry
        Structured bio data extracted from the page.
    """
    if fetched_at is None:
        fetched_at = datetime.now(UTC)

    soup = BeautifulSoup(html, "lxml")

    # Extract biography text (before ToC)
    biography_text = _extract_biography_text(soup)

    # Extract structured sections
    education: list[EducationEntry] = []
    education_section = _extract_section_content(soup, "Education")
    if education_section is not None:
        education = _parse_education(education_section)

    career: list[CareerEntry] = []
    career_section = _extract_section_content(soup, "Career")
    if career_section is not None:
        career = _parse_career(career_section)

    elections: list[ElectionEntry] = []
    elections_section = _extract_section_content(soup, "Elections")
    if elections_section is not None:
        elections = _parse_elections(elections_section)

    logger.info(
        "Parsed Ballotpedia page",
        source_url=source_url,
        education_count=len(education),
        career_count=len(career),
        election_count=len(elections),
        has_bio=biography_text is not None,
    )

    return BallotpediaBioEntry(
        source_url=source_url,
        fetched_at=fetched_at,
        biography_text=biography_text,
        education=education,
        career=career,
        elections=elections,
        raw_html=html,
    )


def is_article_not_found(html: str) -> bool:
    """Check if a Ballotpedia response is a "page not found" result.

    When a page does not exist, Ballotpedia returns a search results
    page with the text "There is no page titled" or "There were no
    results matching the query."

    Parameters
    ----------
    html : str
        The HTML response body.

    Returns
    -------
    bool
        True if the page indicates the article does not exist.
    """
    soup = BeautifulSoup(html, "lxml")

    # Check for "no results" message
    no_results = soup.find("p", class_="mw-search-nonefound")
    if no_results is not None:
        return True

    # Check for "There is no page titled" text
    search_div = soup.find("div", class_="searchresults")
    if search_div is not None:
        text = search_div.get_text()
        if "There is no page titled" in text or "page does not exist" in text:
            return True

    return False


def build_external_s3_key(content_hash: str) -> str:
    """Build S3 key for a Ballotpedia page in the external archive.

    Uses the content-addressed key structure for external sources:
    ``external/ballotpedia/{content_hash}.html``

    Parameters
    ----------
    content_hash : str
        SHA-256 hex digest of the raw HTML content.

    Returns
    -------
    str
        The S3 key for the archived page.
    """
    return f"external/ballotpedia/{content_hash}.html"


def archive_to_s3(
    html: str,
    source_url: str,
    fetched_at: datetime,
    *,
    s3_client: object | None = None,
    bucket: str | None = None,
) -> str | None:
    """Archive raw Ballotpedia HTML to S3, returning the S3 key.

    Uses a HeadObject check to skip upload for unchanged pages
    (content-addressed keys make this idempotent).

    Parameters
    ----------
    html : str
        The raw HTML content to archive.
    source_url : str
        The URL the page was fetched from (stored as S3 metadata).
    fetched_at : datetime
        When the page was fetched (stored as S3 metadata for provenance).
    s3_client : object | None
        An S3 client (or ``CachedS3Client``). Defaults to ``make_s3_client()``.
    bucket : str | None
        The S3 bucket name. Defaults to ``JUDGEMIND_ARCHIVE_BUCKET`` env var.

    Returns
    -------
    str | None
        The S3 key if archival succeeded, or None if S3 is not configured
        (no bucket set).
    """
    if bucket is None:
        bucket = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "")
    if not bucket:
        logger.debug("S3 archival disabled (JUDGEMIND_ARCHIVE_BUCKET not set)")
        return None

    if s3_client is None:
        s3_client = make_s3_client()

    content_bytes = html.encode("utf-8")
    content_hash = sha256_hex(content_bytes)
    key = build_external_s3_key(content_hash)

    # HeadObject check: skip upload if content already archived
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        logger.debug("Already archived s3://%s/%s, skipping upload", bucket, key)
        return key
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "404":
            logger.error("S3 HeadObject failed for %s: %s", key, exc)
            raise

    # Upload the raw HTML
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content_bytes,
            ContentType="text/html",
            Metadata={
                "source": "ballotpedia",
                "source-url": source_url,
                "content-hash": content_hash,
                "fetched-at": fetched_at.isoformat(),
            },
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error("S3 archival failed for %s: %s", key, exc)
        raise
    logger.info("Archived %s bytes to s3://%s/%s", len(content_bytes), bucket, key)
    return key


def rebuild_bio_from_s3(
    s3_key: str,
    *,
    source_url: str | None = None,
    s3_client: object | None = None,
    bucket: str | None = None,
    fetched_at: datetime | None = None,
) -> BallotpediaBioEntry:
    """Re-extract bio data from an archived Ballotpedia page in S3.

    This enables re-extraction when parsing logic improves, without
    needing to re-scrape Ballotpedia. The S3 object metadata contains
    the original ``source-url`` and ``fetched-at`` timestamps, making
    the archive self-contained. These can be overridden via parameters.

    Parameters
    ----------
    s3_key : str
        The S3 key of the archived page (e.g., ``external/ballotpedia/{hash}.html``).
    source_url : str | None
        Override for the original URL. If None, read from S3 metadata.
    s3_client : object | None
        An S3 client. Defaults to ``make_s3_client()``.
    bucket : str | None
        The S3 bucket name. Defaults to ``JUDGEMIND_ARCHIVE_BUCKET`` env var.
    fetched_at : datetime | None
        Override for the original fetch time. If None, read from S3 metadata.
        Falls back to now (UTC) if metadata is also unavailable.

    Returns
    -------
    BallotpediaBioEntry
        Structured bio data re-extracted from the archived page.

    Raises
    ------
    ValueError
        If the bucket is not configured, or if ``source_url`` is not
        provided and not available in S3 metadata.
    ClientError
        If the S3 object cannot be fetched.
    """
    if bucket is None:
        bucket = os.environ.get("JUDGEMIND_ARCHIVE_BUCKET", "")
    if not bucket:
        msg = "JUDGEMIND_ARCHIVE_BUCKET not set — cannot rebuild from S3"
        raise ValueError(msg)

    if s3_client is None:
        s3_client = make_s3_client()

    response = s3_client.get_object(Bucket=bucket, Key=s3_key)
    html = response["Body"].read().decode("utf-8")

    # Read provenance metadata from S3 object, with parameter overrides
    metadata = response.get("Metadata", {})

    if source_url is None:
        source_url = metadata.get("source-url")
    if source_url is None:
        msg = "source_url not provided and not found in S3 metadata — cannot determine page origin"
        raise ValueError(msg)

    if fetched_at is None:
        fetched_at_str = metadata.get("fetched-at")
        if fetched_at_str:
            fetched_at = datetime.fromisoformat(fetched_at_str)
        elif "LastModified" in response:
            # Fallback: use the S3 object's LastModified time as a proxy
            # for the original fetch time when metadata is missing (e.g.,
            # objects uploaded before fetched-at metadata was added).
            fetched_at = response["LastModified"]

    entry = parse_ballotpedia_page(html, source_url, fetched_at)
    entry.s3_key = s3_key

    logger.info(
        "Rebuilt bio from S3",
        s3_key=s3_key,
        source_url=source_url,
        education_count=len(entry.education),
        career_count=len(entry.career),
    )
    return entry


def fetch_judge_bio(
    first_name: str,
    last_name: str,
    middle_initial: str | None = None,
    *,
    timeout: float = 30.0,
    request_delay: float = DEFAULT_REQUEST_DELAY,
    s3_client: object | None = None,
    bucket: str | None = None,
) -> BallotpediaBioEntry | None:
    """Fetch, archive, and parse a judge's Ballotpedia page.

    Tries candidate URLs in order (with middle initial first, then without).
    Archives raw HTML to S3 before parsing (if ``JUDGEMIND_ARCHIVE_BUCKET``
    is set). Returns the first successful result, or None if no page is found.

    Parameters
    ----------
    first_name : str
        The judge's first name.
    last_name : str
        The judge's last name.
    middle_initial : str | None
        The judge's middle initial (e.g., "T" or "T.").
    timeout : float
        HTTP request timeout in seconds.
    request_delay : float
        Delay in seconds between requests (rate limiting).
    s3_client : object | None
        An S3 client for archival. Defaults to ``make_s3_client()``.
    bucket : str | None
        S3 bucket name for archival. Defaults to ``JUDGEMIND_ARCHIVE_BUCKET`` env var.

    Returns
    -------
    BallotpediaBioEntry | None
        Structured bio data if a page was found, or None if the judge
        has no Ballotpedia page.
    """
    urls = build_candidate_urls(first_name, last_name, middle_initial)

    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for i, url in enumerate(urls):
            if i > 0:
                time.sleep(request_delay)

            logger.info("Fetching Ballotpedia page", url=url)
            try:
                response = client.get(url)
            except httpx.HTTPError:
                logger.warning("HTTP error fetching Ballotpedia page", url=url, exc_info=True)
                continue

            if response.status_code == 404:
                logger.debug("Ballotpedia page not found (404)", url=url)
                continue

            # Ballotpedia may return 200 with a search results page for
            # non-existent articles
            if response.status_code == 200 and is_article_not_found(response.text):
                logger.debug("Ballotpedia article not found (search page)", url=url)
                continue

            if response.status_code == 200:
                # Generate timestamp once for consistent provenance
                fetched_at = datetime.now(UTC)

                # Archive raw HTML to S3 before parsing
                s3_key = archive_to_s3(
                    response.text,
                    source_url=url,
                    fetched_at=fetched_at,
                    s3_client=s3_client,
                    bucket=bucket,
                )

                entry = parse_ballotpedia_page(
                    html=response.text,
                    source_url=url,
                    fetched_at=fetched_at,
                )
                entry.s3_key = s3_key
                return entry

            # Unexpected status code — log and continue to next URL
            logger.warning(
                "Unexpected Ballotpedia response",
                url=url,
                status_code=response.status_code,
            )

    logger.info(
        "No Ballotpedia page found for judge",
        first_name=first_name,
        last_name=last_name,
        middle_initial=middle_initial,
        urls_tried=len(urls),
    )
    return None
