"""Kern County Superior Court — Department-to-Judge Mapping.

Scrapes the judicial officers page at:
    https://www.kern.courts.ca.gov/general-information/judicial-officers

The page contains 9 HTML tables (one per courthouse/division):
    Metropolitan, Justice Building, Juvenile Justice Center, Traffic Court,
    Delano, Shafter, Lamont, Mojave, Ridgecrest.

Each table has a header row with columns: Courtroom, Judge/Commissioner,
Assignment.  Data rows use a ``<th>`` cell for the department/courtroom name
(e.g., "Dept. 1", "Div. A", "J1", "T1", "Delano A") and ``<td>`` cells
for the judge name and assignment.

This module provides:
    - ``fetch_department_judge_mapping()`` — scrape the live page and return the mapping
    - ``parse_judicial_officers_html()`` — parse all tables into DepartmentJudge records
    - ``build_department_judge_map()`` — build a dept->judge lookup from records
    - ``lookup_judge_for_department()`` — look up a judge name by department number

Department identifiers are preserved as-is (e.g., "Dept. 1", "Div. A", "J1"),
then normalized by stripping the "Dept. " or "Div. " prefix and leading zeros.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import structlog
from bs4 import BeautifulSoup, Tag

from framework.court_directory import CourtDirectory

from .la_dept_judges import normalize_department

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)

JUDICIAL_OFFICERS_URL = "https://www.kern.courts.ca.gov/general-information/judicial-officers"

# Prefixes to strip from department names for normalization
_DEPT_PREFIX_RE = re.compile(r"^(?:Dept\.\s*|Div\.\s*)", re.IGNORECASE)

# Entries to skip (vacant, under construction, etc.)
_SKIP_NAMES = frozenset({"vacant", "under construction", ""})


@dataclass
class DepartmentJudge:
    """A department-to-judge entry from the Kern judicial officers page."""

    department: str
    judge_name: str
    assignment: str
    courthouse: str


def _normalize_kern_department(raw_dept: str) -> str:
    """Normalize a Kern department identifier.

    Strips "Dept. " or "Div. " prefix if present, then applies the
    standard numeric normalization (strip leading zeros).

    Examples:
        "Dept. 1"            -> "1"
        "Dept. 15"           -> "15"
        "Div. A"             -> "A"
        "J1"                 -> "J1"
        "Presiding Department" -> "Presiding Department"
        "Delano A"           -> "Delano A"
    """
    stripped = _DEPT_PREFIX_RE.sub("", raw_dept).strip()
    return normalize_department(stripped)


def _get_courthouse_label(table: Tag) -> str:
    """Extract the courthouse label from the heading preceding a table.

    Looks for the nearest ``<h2>``, ``<h3>``, or ``<h4>`` element before
    the table and returns its text content.

    Returns "Unknown" if no heading is found.
    """
    prev = table.find_previous(["h2", "h3", "h4"])
    if prev is not None:
        return prev.get_text(strip=True)
    return "Unknown"


def parse_judicial_officers_html(html: str) -> list[DepartmentJudge]:
    """Parse the Kern judicial officers page into DepartmentJudge records.

    Iterates over all ``<table>`` elements on the page. Each table represents
    a courthouse division.  Data rows have a ``<th>`` cell for the department
    name and ``<td>`` cells for judge name and assignment.

    Rows where the judge name is "VACANT" or "Under Construction" are skipped.

    Returns an empty list if no tables or no valid entries are found.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[DepartmentJudge] = []

    for table in soup.find_all("table"):
        courthouse = _get_courthouse_label(table)

        for row in table.find_all("tr"):
            # Skip header-only rows (all <th>, no <td>)
            th_cells = row.find_all("th")
            td_cells = row.find_all("td")

            if not td_cells:
                continue

            # The department name is in the <th> cell of the data row
            if not th_cells:
                continue

            raw_dept = th_cells[0].get_text(strip=True)
            if not raw_dept:
                continue

            # Judge name is in the first <td>
            judge_name = td_cells[0].get_text(strip=True)
            # Normalize whitespace
            judge_name = " ".join(judge_name.split())

            # Skip vacant/placeholder entries
            if judge_name.lower() in _SKIP_NAMES:
                continue

            # Assignment is in the second <td> if present
            assignment = td_cells[1].get_text(strip=True) if len(td_cells) > 1 else ""

            entries.append(
                DepartmentJudge(
                    department=raw_dept,
                    judge_name=judge_name,
                    assignment=assignment,
                    courthouse=courthouse,
                )
            )

    logger.info("Parsed Kern department-judge entries", count=len(entries))
    return entries


def build_department_judge_map(
    entries: list[DepartmentJudge],
) -> dict[str, str]:
    """Build a normalized-department -> judge-name mapping.

    Department identifiers are normalized by stripping "Dept." / "Div."
    prefixes and leading zeros, so lookups work regardless of format.

    Args:
        entries: List of DepartmentJudge records from ``parse_judicial_officers_html``.

    Returns:
        Dict mapping normalized department strings to judge names.
    """
    dept_map: dict[str, str] = {}
    for entry in entries:
        norm_dept = _normalize_kern_department(entry.department)
        if norm_dept not in dept_map:
            dept_map[norm_dept] = entry.judge_name
        else:
            logger.debug(
                "Duplicate department — keeping first entry",
                department=norm_dept,
                existing=dept_map[norm_dept],
                duplicate=entry.judge_name,
            )
    return dept_map


def lookup_judge_for_department(
    dept_map: dict[str, str],
    department: str,
) -> str | None:
    """Look up the judge name for a given department identifier.

    Normalizes the department before lookup. Returns None if
    the department is not in the mapping.

    Args:
        dept_map: The department-to-judge mapping from ``build_department_judge_map``.
        department: The department identifier to look up.

    Returns:
        The judge's name, or None if not found.
    """
    norm = _normalize_kern_department(department)
    return dept_map.get(norm)


def _fetch_and_parse_directory(timeout: float = 30.0) -> tuple[bytes, dict[str, str]]:
    """Fetch and parse the Kern judicial officer directory (shared helper).

    Makes a single HTTP GET to the judicial officers page, parses all
    tables, and builds the department-to-judge mapping.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (raw_response_bytes, dept_to_judge_mapping).

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    logger.info("Fetching Kern judicial officer directory", url=JUDICIAL_OFFICERS_URL)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        response = client.get(JUDICIAL_OFFICERS_URL)
        response.raise_for_status()

    raw = response.content
    entries = parse_judicial_officers_html(response.text)
    dept_map = build_department_judge_map(entries)
    logger.info("Built Kern department-judge mapping", departments=len(dept_map))
    return raw, dept_map


class KernCourtDirectory(CourtDirectory):
    """Kern County Superior Court department-to-judge directory with snapshotting.

    Implements ``CourtDirectory.fetch_current()`` by scraping the judicial
    officer directory page and parsing all 9 courthouse tables into a combined
    department-to-judge mapping.  The base class handles S3 archival, DB storage,
    and content-hash deduplication.

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
    COURT_ID: str = "ca_kern"

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
        """Fetch the live Kern judicial officer directory.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_html_bytes, dept_to_judge_mapping).
        """
        return _fetch_and_parse_directory(self._timeout)

    def fetch_and_snapshot(self, court_id: str | None = None) -> dict[str, str]:
        """Fetch the live directory and save a snapshot.

        Overrides the base class to default ``court_id`` to
        :attr:`COURT_ID` (``"ca_kern"``).

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


def fetch_department_judge_mapping(
    timeout: float = 30.0,
) -> dict[str, str]:
    """Fetch the Kern judicial officer directory and return a dept->judge mapping.

    Makes a single HTTP GET to the judicial officers page, parses all
    courthouse tables, and builds a combined department-to-judge mapping.

    This is a convenience function that does **not** perform snapshotting.
    For production use with archival, use :class:`KernCourtDirectory` instead.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to judge names.

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    _raw, dept_map = _fetch_and_parse_directory(timeout)
    return dept_map
