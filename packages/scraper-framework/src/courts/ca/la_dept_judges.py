"""LA County Superior Court — Department-to-Judge Mapping.

Scrapes the judicial officer directory at:
    https://www.lacourt.ca.gov/judicialofficers/ui/SearchResult.aspx

The page contains a sortable HTML table with columns:
    Last Name, First Name, Title, Courthouse, Department, Phone, Primary Assignment

This module provides:
    - ``fetch_department_judge_mapping()`` — scrape the live page and return the mapping
    - ``parse_judicial_officers_html()`` — parse HTML into JudicialOfficer records
    - ``build_department_judge_map()`` — build a dept→judge lookup from officer records
    - ``lookup_judge_for_department()`` — look up a judge name by department number

The mapping is used by the LA tentative rulings scraper to populate judge names
for rulings that don't include the judge's name in the PDF/HTML content.

Department numbers are normalized by stripping leading zeros for comparison
(e.g., "052" → "52", "005" → "5"), since the tentative rulings dropdown
uses unpadded numbers while the directory uses zero-padded numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import structlog
from bs4 import BeautifulSoup

from framework.court_directory import CourtDirectory

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)

JUDICIAL_OFFICERS_URL = "https://www.lacourt.ca.gov/judicialofficers/ui/SearchResult.aspx"

# Table id on the judicial officers page
TABLE_ID = "GridView1"


@dataclass
class JudicialOfficer:
    """A single judicial officer from the LA Court directory."""

    last_name: str
    first_name: str
    title: str  # "Judge" or "Commissioner"
    courthouse: str
    department: str
    phone: str
    primary_assignment: str

    @property
    def full_name(self) -> str:
        """Return the officer's full name as 'First Last'."""
        return f"{self.first_name} {self.last_name}"


def normalize_department(dept: str) -> str:
    """Normalize a department number by stripping leading zeros.

    The judicial officers directory uses zero-padded numbers (e.g., "052", "005")
    while the tentative rulings dropdown uses unpadded numbers (e.g., "52", "5").
    Alphanumeric departments like "F46" or "H" are returned as-is.

    Examples:
        "052" → "52"
        "005" → "5"
        "3"   → "3"
        "F46" → "F46"
        "H"   → "H"
    """
    # Only strip zeros from purely numeric departments
    if dept.isdigit():
        return str(int(dept))
    return dept.strip()


def parse_judicial_officers_html(html: str) -> list[JudicialOfficer]:
    """Parse the judicial officers HTML page into a list of JudicialOfficer records.

    Expects a page containing a table with id ``GridView1`` and columns:
    Last Name, First Name, Title, Courthouse, Department, Phone, Primary Assignment.

    Returns an empty list if the table is not found or has no data rows.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id=TABLE_ID)
    if table is None:
        logger.warning("Judicial officers table not found", table_id=TABLE_ID)
        return []

    officers: list[JudicialOfficer] = []
    # Skip the header row (thead/tr), iterate data rows in tbody
    tbody = table.find("tbody")
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        officer = JudicialOfficer(
            last_name=cells[0].get_text(strip=True),
            first_name=cells[1].get_text(strip=True),
            title=cells[2].get_text(strip=True),
            courthouse=cells[3].get_text(strip=True),
            department=cells[4].get_text(strip=True),
            phone=cells[5].get_text(strip=True),
            primary_assignment=cells[6].get_text(strip=True),
        )
        officers.append(officer)

    logger.info("Parsed judicial officers", count=len(officers))
    return officers


def build_department_judge_map(
    officers: list[JudicialOfficer],
) -> dict[str, str]:
    """Build a normalized-department → judge-full-name mapping.

    Department numbers are normalized (leading zeros stripped) so lookups
    work regardless of padding. If multiple officers share the same department,
    the first one encountered wins (the directory is authoritative).

    Args:
        officers: List of JudicialOfficer records from ``parse_judicial_officers_html``.

    Returns:
        Dict mapping normalized department strings to full judge names.
    """
    dept_map: dict[str, str] = {}
    for officer in officers:
        norm_dept = normalize_department(officer.department)
        if norm_dept not in dept_map:
            dept_map[norm_dept] = officer.full_name
        else:
            logger.debug(
                "Duplicate department — keeping first officer",
                department=norm_dept,
                existing=dept_map[norm_dept],
                duplicate=officer.full_name,
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
        The judge's full name, or None if not found.
    """
    norm = normalize_department(department)
    return dept_map.get(norm)


def _fetch_and_parse_directory(timeout: float = 30.0) -> tuple[bytes, dict[str, str]]:
    """Fetch and parse the LA judicial officer directory (shared helper).

    Makes a single HTTP GET to the judicial officers page, parses the
    officer table, and builds the department-to-judge mapping.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (raw_response_bytes, dept_to_judge_mapping).

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    logger.info("Fetching LA judicial officer directory", url=JUDICIAL_OFFICERS_URL)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        response = client.get(JUDICIAL_OFFICERS_URL)
        response.raise_for_status()

    raw = response.content
    officers = parse_judicial_officers_html(response.text)
    dept_map = build_department_judge_map(officers)
    logger.info("Built department-judge mapping", departments=len(dept_map))
    return raw, dept_map


class LACourtDirectory(CourtDirectory):
    """LA County Superior Court department-to-judge directory with snapshotting.

    Implements ``CourtDirectory.fetch_current()`` by scraping the judicial
    officer directory page and parsing the HTML table into a department-to-judge
    mapping.  The base class handles S3 archival, DB storage, and content-hash
    deduplication.

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
    COURT_ID: str = "ca_los_angeles"

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
        """Fetch the live LA judicial officer directory.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_html_bytes, dept_to_judge_mapping).
        """
        return _fetch_and_parse_directory(self._timeout)

    def fetch_and_snapshot(self, court_id: str | None = None) -> dict[str, str]:
        """Fetch the live directory and save a snapshot.

        Overrides the base class to default ``court_id`` to
        :attr:`COURT_ID` (``"ca_los_angeles"``).

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
    """Fetch the LA judicial officer directory and return a dept→judge mapping.

    Makes a single HTTP GET to the judicial officers page, parses the
    officer table, and builds the department-to-judge mapping.

    This is a convenience function that does **not** perform snapshotting.
    For production use with archival, use :class:`LACourtDirectory` instead.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to full judge names.

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    _raw, dept_map = _fetch_and_parse_directory(timeout)
    return dept_map
