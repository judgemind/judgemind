"""Orange County Superior Court — Department-to-Judge Mapping.

Scrapes the judicial officers page at:
    https://www.occourts.org/general-information/judicial-officers

The page contains a single HTML table with columns:
    Judges, Panel, Floor, Dept., Phone

The "Judges" column contains names in "LAST, FIRST" format (e.g., "ADAMS, JOHN S.").
The "Dept." column contains department codes that encode the courthouse location
(H=Harbor, W=West, L=Lamoreaux, etc.).

This module provides:
    - ``fetch_department_judge_mapping()`` — scrape the live page and return the mapping
    - ``parse_judicial_officers_html()`` — parse HTML into JudicialOfficer records
    - ``build_department_judge_map()`` — build a dept->judge lookup from officer records
    - ``lookup_judge_for_department()`` — look up a judge name by department number

The mapping is used by OC tentative rulings scrapers to populate judge names
for rulings that only contain the department number.

Department numbers are normalized by stripping leading zeros for comparison,
using the same normalization as the LA mapping module.
"""

from __future__ import annotations

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

JUDICIAL_OFFICERS_URL = "https://www.occourts.org/general-information/judicial-officers"


@dataclass
class JudicialOfficer:
    """A single judicial officer from the OC Court directory."""

    name: str
    panel: str
    floor: str
    department: str
    phone: str


def _parse_last_first_name(raw: str) -> str:
    """Convert "LAST, FIRST" to "First Last" format.

    Handles multi-word last names and suffixes.  Title-cases the result
    so that "ADAMS, JOHN S." becomes "John S. Adams".

    Examples:
        "ADAMS, JOHN S."         -> "John S. Adams"
        "ALVAREZ, CLAUDIA C."    -> "Claudia C. Alvarez"
        "DE LA TORRE, MARICELA"  -> "Maricela De La Torre"
        "SMITH"                  -> "Smith"
    """
    raw = raw.strip()
    if not raw:
        return ""
    if "," in raw:
        last, first = raw.split(",", 1)
        last = last.strip().title()
        first = first.strip().title()
        return f"{first} {last}"
    return raw.title()


def parse_judicial_officers_html(html: str) -> list[JudicialOfficer]:
    """Parse the OC judicial officers HTML page into a list of JudicialOfficer records.

    Finds the first ``<table>`` on the page and extracts officers from rows
    with columns: Judges, Panel, Floor, Dept., Phone.

    Returns an empty list if no suitable table is found or has no data rows.
    """
    soup = BeautifulSoup(html, "lxml")
    officers: list[JudicialOfficer] = []

    table = soup.find("table")
    if table is None:
        logger.warning("No table found on OC judicial officers page")
        return []

    # Find header row to determine column indices
    headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
    if not headers:
        logger.warning("No header row found in OC judicial officers table")
        return []

    # Map column names to indices
    col_map: dict[str, int] = {}
    for i, h in enumerate(headers):
        if "judge" in h:
            col_map["name"] = i
        elif "panel" in h:
            col_map["panel"] = i
        elif "floor" in h:
            col_map["floor"] = i
        elif "dept" in h:
            col_map["dept"] = i
        elif "phone" in h:
            col_map["phone"] = i

    if "name" not in col_map or "dept" not in col_map:
        logger.warning(
            "Required columns not found in OC table",
            found_headers=headers,
        )
        return []

    # Parse data rows (skip header row)
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) <= max(col_map.values()):
            continue

        raw_name = cells[col_map["name"]].get_text(strip=True)
        department = cells[col_map["dept"]].get_text(strip=True)

        # Skip empty names or departments
        if not raw_name or not department:
            continue

        name = _parse_last_first_name(raw_name)
        # Normalize whitespace
        name = " ".join(name.split())

        panel = cells[col_map.get("panel", 1)].get_text(strip=True) if "panel" in col_map else ""
        floor = cells[col_map.get("floor", 2)].get_text(strip=True) if "floor" in col_map else ""
        # Replace non-breaking spaces in phone numbers
        phone = (
            cells[col_map["phone"]].get_text(strip=True).replace("\xa0", " ")
            if "phone" in col_map
            else ""
        )

        officers.append(
            JudicialOfficer(
                name=name,
                panel=panel,
                floor=floor,
                department=department,
                phone=phone,
            )
        )

    logger.info("Parsed OC judicial officers", count=len(officers))
    return officers


def build_department_judge_map(
    officers: list[JudicialOfficer],
) -> dict[str, str]:
    """Build a normalized-department -> judge-name mapping.

    Department codes are normalized (leading zeros stripped for numeric parts)
    so lookups work regardless of padding.

    Args:
        officers: List of JudicialOfficer records from ``parse_judicial_officers_html``.

    Returns:
        Dict mapping normalized department strings to judge names.
    """
    dept_map: dict[str, str] = {}
    for officer in officers:
        norm_dept = normalize_department(officer.department)
        if norm_dept not in dept_map:
            dept_map[norm_dept] = officer.name
        else:
            logger.debug(
                "Duplicate department — keeping first officer",
                department=norm_dept,
                existing=dept_map[norm_dept],
                duplicate=officer.name,
            )
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
    """Fetch and parse the OC judicial officer directory (shared helper).

    Makes a single HTTP GET to the judicial officers page, parses the
    officer table, and builds the department-to-judge mapping.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (raw_response_bytes, dept_to_judge_mapping).

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    logger.info("Fetching OC judicial officer directory", url=JUDICIAL_OFFICERS_URL)
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
    logger.info("Built OC department-judge mapping", departments=len(dept_map))
    return raw, dept_map


class OCCourtDirectory(CourtDirectory):
    """Orange County Superior Court department-to-judge directory with snapshotting.

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
    COURT_ID: str = "ca_orange"

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
        """Fetch the live OC judicial officer directory.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_html_bytes, dept_to_judge_mapping).
        """
        return _fetch_and_parse_directory(self._timeout)

    def fetch_and_snapshot(self, court_id: str | None = None) -> dict[str, str]:
        """Fetch the live directory and save a snapshot.

        Overrides the base class to default ``court_id`` to
        :attr:`COURT_ID` (``"ca_orange"``).

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
    """Fetch the OC judicial officer directory and return a dept->judge mapping.

    Makes a single HTTP GET to the judicial officers page, parses the
    officer table, and builds the department-to-judge mapping.

    This is a convenience function that does **not** perform snapshotting.
    For production use with archival, use :class:`OCCourtDirectory` instead.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to judge names.

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    _raw, dept_map = _fetch_and_parse_directory(timeout)
    return dept_map
