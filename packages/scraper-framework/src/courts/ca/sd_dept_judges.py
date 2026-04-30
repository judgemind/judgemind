"""San Diego County Superior Court — Department-to-Judge Mapping.

Scrapes the judge assignments page at:
    https://www.sdcourt.ca.gov/sdcourt/generalinformation/judgeassignments

The page contains a single HTML table with columns:
    Department Policies and Procedures, File Link.

The first column combines the department code and judge name in a single cell:
    "Department N-18Hon. Renee N.G. Stackhouse"

A regex splits the combined text into department code and judge name::

    Department\\s+(\\S+)\\s*Hon\\.\\s*(.+)

This module provides:
    - ``fetch_department_judge_mapping()`` — scrape the live page and return the mapping
    - ``parse_judge_assignments_html()`` — parse HTML into DepartmentJudge records
    - ``build_department_judge_map()`` — build a dept->judge lookup from records
    - ``lookup_judge_for_department()`` — look up a judge name by department number

Department codes are normalized by stripping leading zeros for comparison,
using the same normalization as the LA mapping module.

SD convention: purely-numeric department codes in the assignments page (e.g. "64")
correspond to the Central courthouse, which the civil calendar encodes explicitly
as "C-NN" (e.g. "C-64").  ``build_department_judge_map`` stores both keys so
lookups from either source format succeed.
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

JUDGE_ASSIGNMENTS_URL = "https://www.sdcourt.ca.gov/sdcourt/generalinformation/judgeassignments"

# Pattern to split "Department N-18Hon. Renee N.G. Stackhouse"
_DEPT_JUDGE_RE = re.compile(
    r"Department\s+(?P<department>\S+)\s*Hon\.\s*(?P<judge_name>.+)",
    re.IGNORECASE,
)


@dataclass
class DepartmentJudge:
    """A department-to-judge entry from the San Diego judge assignments page."""

    department: str
    judge_name: str


def parse_judge_assignments_html(html: str) -> list[DepartmentJudge]:
    """Parse the San Diego judge assignments page into DepartmentJudge records.

    Finds the first ``<table>`` on the page and extracts department-to-judge
    mappings from the first column of each data row by splitting the combined
    "Department {code}Hon. {name}" text.

    Returns an empty list if no table is found or no entries match the pattern.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[DepartmentJudge] = []

    table = soup.find("table")
    if table is None:
        logger.warning("No table found on San Diego judge assignments page")
        return []

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue

        # The first cell contains the combined dept + judge text
        cell_text = cells[0].get_text(strip=True)
        # Normalize non-breaking spaces
        cell_text = cell_text.replace("\xa0", " ")

        m = _DEPT_JUDGE_RE.search(cell_text)
        if not m:
            continue

        department = m.group("department").strip()
        raw_name = m.group("judge_name").strip()
        # Normalize whitespace in judge name
        judge_name = " ".join(raw_name.split())

        if not department or not judge_name:
            continue

        entries.append(
            DepartmentJudge(
                department=department,
                judge_name=judge_name,
            )
        )

    logger.info("Parsed San Diego department-judge entries", count=len(entries))
    return entries


def build_department_judge_map(
    entries: list[DepartmentJudge],
) -> dict[str, str]:
    """Build a normalized-department -> judge-name mapping.

    Department codes are normalized (leading zeros stripped for numeric parts)
    so lookups work regardless of padding.

    Args:
        entries: List of DepartmentJudge records from ``parse_judge_assignments_html``.

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
                "Duplicate department — keeping first entry",
                department=norm_dept,
                existing=dept_map[norm_dept],
                duplicate=entry.judge_name,
            )
        # SD convention: bare-numeric dept in the assignments page = Central courthouse;
        # the calendar writes C-NN explicitly, so alias both keys.
        if norm_dept.isdigit() and f"C-{norm_dept}" not in dept_map:
            dept_map[f"C-{norm_dept}"] = entry.judge_name
    return dept_map


def lookup_judge_for_department(
    dept_map: dict[str, str],
    department: str,
) -> str | None:
    """Look up the judge name for a given department code.

    Normalizes the department code before lookup. Returns None if
    the department is not in the mapping.

    Args:
        dept_map: The department-to-judge mapping from ``build_department_judge_map``.
        department: The department code to look up.

    Returns:
        The judge's name, or None if not found.
    """
    norm = normalize_department(department)
    return dept_map.get(norm)


def _fetch_and_parse_directory(timeout: float = 30.0) -> tuple[bytes, dict[str, str]]:
    """Fetch and parse the San Diego judge assignments page (shared helper).

    Makes a single HTTP GET to the judge assignments page, parses the
    table, and builds the department-to-judge mapping.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (raw_response_bytes, dept_to_judge_mapping).

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    logger.info("Fetching San Diego judge assignments", url=JUDGE_ASSIGNMENTS_URL)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        response = client.get(JUDGE_ASSIGNMENTS_URL)
        response.raise_for_status()

    raw = response.content
    entries = parse_judge_assignments_html(response.text)
    dept_map = build_department_judge_map(entries)
    logger.info("Built San Diego department-judge mapping", departments=len(dept_map))
    return raw, dept_map


class SanDiegoCourtDirectory(CourtDirectory):
    """San Diego County Superior Court department-to-judge directory with snapshotting.

    Implements ``CourtDirectory.fetch_current()`` by scraping the judge
    assignments page and parsing the combined department/judge cells into a
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
    COURT_ID: str = "ca_san_diego"

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
        """Fetch the live San Diego judge assignments directory.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_html_bytes, dept_to_judge_mapping).
        """
        return _fetch_and_parse_directory(self._timeout)

    def fetch_and_snapshot(self, court_id: str | None = None) -> dict[str, str]:
        """Fetch the live directory and save a snapshot.

        Overrides the base class to default ``court_id`` to
        :attr:`COURT_ID` (``"ca_san_diego"``).

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
    """Fetch the San Diego judge assignments and return a dept->judge mapping.

    Makes a single HTTP GET to the judge assignments page, parses the
    table, and builds the department-to-judge mapping.

    This is a convenience function that does **not** perform snapshotting.
    For production use with archival, use :class:`SanDiegoCourtDirectory` instead.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to judge names.

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    _raw, dept_map = _fetch_and_parse_directory(timeout)
    return dept_map
