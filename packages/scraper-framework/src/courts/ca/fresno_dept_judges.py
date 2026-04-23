"""Fresno County Superior Court — Department-to-Judge Mapping.

Scrapes the judicial assignments page at:
    https://www.fresno.courts.ca.gov/general-information/judicial-assignments

The page contains 5 HTML tables (one per courthouse location):
    Downtown, Jail Annex, M Street Courthouse, B.F. Sisk Court, Juvenile Justice.

Tables have varying column layouts but all share these columns:
    Department / Courthouse, Judge / Commissioner, Phone Number, Assignment.
Some tables also include a "Location" column.

Department values are in "Dept. N" format (e.g., "Dept. 1", "Dept. 201",
"Dept. 97A").  The "Dept. " prefix is stripped for the normalized mapping.

This module provides:
    - ``fetch_department_judge_mapping()`` — scrape the live page and return the mapping
    - ``parse_judicial_assignments_html()`` — parse all tables into DepartmentJudge records
    - ``build_department_judge_map()`` — build a dept->judge lookup from records
    - ``lookup_judge_for_department()`` — look up a judge name by department number

Department numbers are normalized by stripping the "Dept. " prefix and
leading zeros, using the same normalization as the LA mapping module.
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

JUDICIAL_ASSIGNMENTS_URL = (
    "https://www.fresno.courts.ca.gov/general-information/judicial-assignments"
)

# Strip "Dept. " prefix from department values
_DEPT_PREFIX_RE = re.compile(r"^Dept\.\s*", re.IGNORECASE)


@dataclass
class DepartmentJudge:
    """A department-to-judge entry from the Fresno judicial assignments page."""

    department: str
    judge_name: str
    phone: str
    assignment: str
    courthouse: str


def _normalize_fresno_department(raw_dept: str) -> str:
    """Normalize a Fresno department identifier.

    Strips the "Dept. " prefix if present, then applies the standard
    numeric normalization (strip leading zeros).

    Examples:
        "Dept. 1"     -> "1"
        "Dept. 201"   -> "201"
        "Dept. 97A"   -> "97A"
        "Dept. 99B"   -> "99B"
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


def parse_judicial_assignments_html(html: str) -> list[DepartmentJudge]:
    """Parse the Fresno judicial assignments page into DepartmentJudge records.

    Iterates over all ``<table>`` elements on the page. Each table represents
    a courthouse location with columns for department, judge, phone, and assignment.
    Some tables include a "Location" column.

    Returns an empty list if no tables or no valid entries are found.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[DepartmentJudge] = []

    for table in soup.find_all("table"):
        courthouse = _get_courthouse_label(table)

        # Determine column indices from headers
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers:
            continue

        # Map columns by keyword matching
        col_map: dict[str, int] = {}
        for i, h in enumerate(headers):
            if "department" in h:
                col_map["dept"] = i
            elif "judge" in h or "commissioner" in h:
                col_map["judge"] = i
            elif "phone" in h:
                col_map["phone"] = i
            elif "assignment" in h:
                col_map["assignment"] = i

        if "dept" not in col_map or "judge" not in col_map:
            continue

        # Parse data rows
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue
            if len(cells) <= max(col_map.values()):
                continue

            raw_dept = cells[col_map["dept"]].get_text(strip=True)
            judge_name = cells[col_map["judge"]].get_text(strip=True)

            if not raw_dept or not judge_name:
                continue

            # Normalize whitespace in judge name
            judge_name = " ".join(judge_name.split())

            phone = cells[col_map["phone"]].get_text(strip=True) if "phone" in col_map else ""
            assignment = (
                cells[col_map["assignment"]].get_text(strip=True) if "assignment" in col_map else ""
            )

            entries.append(
                DepartmentJudge(
                    department=raw_dept,
                    judge_name=judge_name,
                    phone=phone,
                    assignment=assignment,
                    courthouse=courthouse,
                )
            )

    logger.info("Parsed Fresno department-judge entries", count=len(entries))
    return entries


def build_department_judge_map(
    entries: list[DepartmentJudge],
) -> dict[str, str]:
    """Build a normalized-department -> judge-name mapping.

    Department identifiers are normalized by stripping the "Dept." prefix
    and leading zeros, so lookups work regardless of format.

    Args:
        entries: List of DepartmentJudge records from ``parse_judicial_assignments_html``.

    Returns:
        Dict mapping normalized department strings to judge names.
    """
    dept_map: dict[str, str] = {}
    for entry in entries:
        norm_dept = _normalize_fresno_department(entry.department)
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
    norm = _normalize_fresno_department(department)
    return dept_map.get(norm)


def _fetch_and_parse_directory(timeout: float = 30.0) -> tuple[bytes, dict[str, str]]:
    """Fetch and parse the Fresno judicial assignments directory (shared helper).

    Makes a single HTTP GET to the judicial assignments page, parses all
    tables, and builds the department-to-judge mapping.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (raw_response_bytes, dept_to_judge_mapping).

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    logger.info("Fetching Fresno judicial assignments", url=JUDICIAL_ASSIGNMENTS_URL)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        response = client.get(JUDICIAL_ASSIGNMENTS_URL)
        response.raise_for_status()

    raw = response.content
    entries = parse_judicial_assignments_html(response.text)
    dept_map = build_department_judge_map(entries)
    logger.info("Built Fresno department-judge mapping", departments=len(dept_map))
    return raw, dept_map


class FresnoCourtDirectory(CourtDirectory):
    """Fresno County Superior Court department-to-judge directory with snapshotting.

    Implements ``CourtDirectory.fetch_current()`` by scraping the judicial
    assignments page and parsing all 5 courthouse tables into a combined
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
    COURT_ID: str = "ca_fresno"

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
        """Fetch the live Fresno judicial assignments directory.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_html_bytes, dept_to_judge_mapping).
        """
        return _fetch_and_parse_directory(self._timeout)

    def fetch_and_snapshot(self, court_id: str | None = None) -> dict[str, str]:
        """Fetch the live directory and save a snapshot.

        Overrides the base class to default ``court_id`` to
        :attr:`COURT_ID` (``"ca_fresno"``).

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
    """Fetch the Fresno judicial assignments and return a dept->judge mapping.

    Makes a single HTTP GET to the judicial assignments page, parses all
    courthouse tables, and builds a combined department-to-judge mapping.

    This is a convenience function that does **not** perform snapshotting.
    For production use with archival, use :class:`FresnoCourtDirectory` instead.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to judge names.

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    _raw, dept_map = _fetch_and_parse_directory(timeout)
    return dept_map
