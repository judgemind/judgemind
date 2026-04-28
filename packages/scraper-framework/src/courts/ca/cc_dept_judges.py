"""Contra Costa County Superior Court -- Department-to-Judge Mapping.

Scrapes department-to-judge mappings from the Contra Costa tentative rulings
index page:
    https://retired.cc-courts.org/civil/motions-hearings-tentative.aspx

Each ``<div class='tr-dir'>`` block on the page contains a ``<span>`` header
with the format::

    Department NN - Judge Name
    Department NN - Judge Name (Probate)
    Department NN - Comm Name

The mapping is extracted from these headers, with a URL-path fallback for any
block whose span text is absent or unparseable (reusing the
``_URL_DEPT_JUDGE_RE`` pattern already established in ``cc_tentatives.py``).

Source selection rationale
--------------------------
A prior investigation (docs/investigations/judge-roster-sources.md, lines
205-213) concluded that Contra Costa has **no dedicated judicial-officer
roster page** (the candidate URLs cc-courts.org/general-info/judicial-
officers.aspx and similar were checked and rejected). The only reliable
public source of dept-to-judge mapping is this same tentative-rulings index
page, consistent with ``SantaClaraCourtDirectory`` in ``sc_tentatives.py``.

This module provides:
    - ``parse_index_page_html()`` -- extract DepartmentJudge records from the
      index-page HTML.
    - ``build_department_judge_map()`` -- build a dept->judge lookup.
    - ``lookup_judge_for_department()`` -- look up a judge by department.
    - ``_fetch_and_parse_directory()`` -- HTTP GET + parse (shared helper).
    - ``fetch_department_judge_mapping()`` -- convenience wrapper.
    - ``ContraCostaCourtDirectory`` -- CourtDirectory subclass with S3
      archival and DB snapshotting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import structlog
from bs4 import BeautifulSoup

from framework.court_directory import CourtDirectory

from .la_dept_judges import normalize_department

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)

INDEX_URL = "https://retired.cc-courts.org/civil/motions-hearings-tentative.aspx"

# Regex for the <span> header inside a tr-dir block.
# Matches: "Department NN - Judge Name", "Department NN - Comm Name",
#          "Department NN - Judge Name (Probate)", etc.
# Group "department" captures the zero-padded number.
# Group "name_raw" captures everything after the dash separator.
_HEADER_RE = re.compile(
    r"Department\s+(?P<department>\d+)\s*-\s*(?P<name_raw>.+)",
    re.IGNORECASE,
)

# Prefixes to strip from the name portion when "Judge" or "Comm" appears.
# e.g. "Judge Reyes" -> "Reyes", "Comm Yamamoto" -> "Yamamoto"
_NAME_PREFIX_RE = re.compile(
    r"^(?:Judge|Comm(?:issioner)?)\s+",
    re.IGNORECASE,
)

# Suffixes to strip from judge names (case-insensitive).
_PROBATE_SUFFIX_RE = re.compile(r"\s*\(Probate\)\s*$", re.IGNORECASE)

# URL-path fallback: "Department 16 - Judge Reyes" inside an href attribute.
# Reused from cc_tentatives.py (same source page).
_URL_DEPT_JUDGE_RE = re.compile(
    r"Department\s+(?P<department>\d+)\s*-\s*Judge\s+(?P<judge_name>[^/\\]+)",
    re.IGNORECASE,
)


@dataclass
class DepartmentJudge:
    """A department-to-judge entry extracted from the CC index page."""

    department: str
    judge_name: str


def _clean_name(raw_name: str) -> str:
    """Strip title prefix and Probate suffix from a raw name string.

    Examples::

        "Judge Reyes"           -> "Reyes"
        "Comm Yamamoto"         -> "Yamamoto"
        "Judge George (Probate)"-> "George"
        "Leonard Marquez"       -> "Leonard Marquez"

    Args:
        raw_name: The text after "Department NN - " in the header.

    Returns:
        The cleaned judge/commissioner name, or empty string if blank.
    """
    name = raw_name.strip()
    # Strip "(Probate)" or similar suffixes
    name = _PROBATE_SUFFIX_RE.sub("", name).strip()
    # Strip "Judge " / "Comm " / "Commissioner " prefix
    name = _NAME_PREFIX_RE.sub("", name).strip()
    # Normalize internal whitespace (e.g. double spaces)
    name = " ".join(name.split())
    return name


def parse_index_page_html(html: str) -> list[DepartmentJudge]:
    """Parse the CC tentative rulings index page for dept-to-judge mappings.

    Iterates over ``<div class='tr-dir'>`` blocks. For each block:

    1. Reads the header ``<span>`` text and applies ``_HEADER_RE``.
    2. If the header is absent or unparseable, falls back to matching
       ``_URL_DEPT_JUDGE_RE`` against the ``href`` of the first
       ``<a class='tentative-ruling'>`` link in the block.

    Blocks that yield no department/judge pair are silently skipped.

    Args:
        html: The raw HTML of the index page.

    Returns:
        A list of DepartmentJudge records (one per unique, parseable block).
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[DepartmentJudge] = []
    seen: set[str] = set()

    for div in soup.find_all("div", class_="tr-dir"):
        department: str | None = None
        judge_name: str | None = None

        # --- Primary path: parse the bold <span> header ---
        span = div.find("span", style=lambda s: s and "font-weight:bold" in s)
        if span:
            header_text = span.get_text(separator=" ", strip=True)
            m = _HEADER_RE.match(header_text)
            if m:
                department = m.group("department")
                judge_name = _clean_name(m.group("name_raw"))

        # --- Fallback path: extract from the first .tentative-ruling href ---
        if not department or not judge_name:
            link = div.find("a", class_="tentative-ruling")
            if link:
                href = link.get("href", "")
                # Normalize Windows backslashes
                href_norm = href.replace("\\", "/")
                fm = _URL_DEPT_JUDGE_RE.search(href_norm)
                if fm:
                    department = fm.group("department")
                    raw = fm.group("judge_name").strip()
                    judge_name = _PROBATE_SUFFIX_RE.sub("", raw).strip()
                    judge_name = " ".join(judge_name.split())

        if not department or not judge_name:
            continue

        norm = normalize_department(department)
        if norm in seen:
            logger.debug(
                "Duplicate CC department -- skipping",
                department=norm,
                judge_name=judge_name,
            )
            continue
        seen.add(norm)
        entries.append(DepartmentJudge(department=department, judge_name=judge_name))

    logger.info("Parsed CC department-judge entries", count=len(entries))
    return entries


def build_department_judge_map(
    entries: list[DepartmentJudge],
) -> dict[str, str]:
    """Build a normalized-department -> judge-name mapping.

    Department numbers are normalized (leading zeros stripped) so lookups
    work regardless of padding.

    Args:
        entries: List of DepartmentJudge records from ``parse_index_page_html``.

    Returns:
        Dict mapping normalized department strings to judge names.
    """
    dept_map: dict[str, str] = {}
    for entry in entries:
        norm_dept = normalize_department(entry.department)
        if norm_dept not in dept_map:
            dept_map[norm_dept] = entry.judge_name
        else:
            logger.debug(
                "Duplicate CC department -- keeping first entry",
                department=norm_dept,
                existing=dept_map[norm_dept],
                duplicate=entry.judge_name,
            )
    return dept_map


def lookup_judge_for_department(
    dept_map: dict[str, str],
    department: str,
) -> str | None:
    """Look up the judge name for a given department number.

    Normalizes the department number before lookup. Returns None if
    the department is not in the mapping.

    Args:
        dept_map: The department-to-judge mapping from ``build_department_judge_map``.
        department: The department number to look up (may have leading zeros).

    Returns:
        The judge's name, or None if not found.
    """
    norm = normalize_department(department)
    return dept_map.get(norm)


def _fetch_and_parse_directory(
    timeout: float = 30.0,
) -> tuple[bytes, dict[str, str]]:
    """Fetch and parse the CC tentative rulings index page (shared helper).

    Makes a single HTTP GET request to the index page, parses the
    ``<div class='tr-dir'>`` blocks, and builds a department-to-judge mapping.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (raw_response_bytes, dept_to_judge_mapping).

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    logger.info("Fetching CC tentative rulings index", url=INDEX_URL)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        response = client.get(INDEX_URL)
        response.raise_for_status()

    raw = response.content
    entries = parse_index_page_html(response.text)
    dept_map = build_department_judge_map(entries)
    logger.info("Built CC department-judge mapping", departments=len(dept_map))
    return raw, dept_map


def fetch_department_judge_mapping(
    timeout: float = 30.0,
) -> dict[str, str]:
    """Fetch the CC tentative rulings index and return a dept->judge mapping.

    Makes a single HTTP GET request to the index page and builds a
    department-to-judge mapping from the ``<div class='tr-dir'>`` headers.

    This is a convenience function that does **not** perform snapshotting.
    For production use with archival, use :class:`ContraCostaCourtDirectory`
    instead.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to judge names.

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    _raw, dept_map = _fetch_and_parse_directory(timeout)
    return dept_map


class ContraCostaCourtDirectory(CourtDirectory):
    """Contra Costa County Superior Court department-to-judge directory with snapshotting.

    Implements ``CourtDirectory.fetch_current()`` by fetching the CC tentative
    rulings index page and extracting department-to-judge mappings from the
    ``<div class='tr-dir'>`` blocks.  The base class handles S3 archival, DB
    storage, and content-hash deduplication.

    Source: https://retired.cc-courts.org/civil/motions-hearings-tentative.aspx

    Per the prior investigation (docs/investigations/judge-roster-sources.md:
    205-213), CC has no dedicated judicial-officer roster page.  The tentative
    rulings index is the only reliable public source of dept-to-judge mappings,
    consistent with the ``SantaClaraCourtDirectory`` pattern in
    ``sc_tentatives.py``.

    Parameters
    ----------
    s3_client : object
        A boto3 S3 client for archiving raw directory responses.
    s3_bucket : str
        The S3 bucket name for archival.
    db_conn : psycopg.Connection
        A psycopg3 connection for reading/writing snapshots.
    timeout : float
        HTTP request timeout in seconds (default 30.0).
    """

    #: Court identifier used for S3 keys and DB records.
    COURT_ID: str = "ca_contra_costa"

    def __init__(
        self,
        s3_client: object,
        s3_bucket: str,
        db_conn: psycopg.Connection,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(s3_client, s3_bucket, db_conn)
        self._timeout = timeout

    def fetch_current(self) -> tuple[bytes, dict[str, str]]:
        """Fetch the live CC tentative rulings index for dept-to-judge mappings.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_html_bytes, dept_to_judge_mapping).
        """
        return _fetch_and_parse_directory(self._timeout)

    def fetch_and_snapshot(self, court_id: str | None = None) -> dict[str, str]:
        """Fetch the live directory and save a snapshot.

        Overrides the base class to default ``court_id`` to
        :attr:`COURT_ID` (``"ca_contra_costa"``).

        Parameters
        ----------
        court_id : str | None
            The court identifier.  Defaults to ``COURT_ID``.

        Returns
        -------
        dict[str, str]
            The parsed {department: judge_name} mapping.
        """
        return super().fetch_and_snapshot(court_id or self.COURT_ID)
