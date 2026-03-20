"""Ventura County Superior Court — Department-to-Judge Mapping.

Scrapes the judicial assignment pages at:
    https://www.ventura.courts.ca.gov/judicial-assignments-ventura  (main courthouse)
    https://www.ventura.courts.ca.gov/judicial-assignments-juvenile (juvenile courthouse)
    https://www.ventura.courts.ca.gov/judicial-assignments-east-county (east county)

Each page contains an HTML table (class ``tablesorter``) with columns:
    Courtroom, Judicial Officer, Title, Calendar Type

The "Courtroom" column contains department identifiers like "20", "42", "J6", "S1".
The "Judicial Officer" column contains names prefixed with "Hon." — e.g.,
"Hon. Ronda J. McKaig".

This module provides:
    - ``fetch_department_judge_mapping()`` — scrape all three pages and return the mapping
    - ``parse_assignments_page_html()`` — parse one HTML page into DepartmentJudge records
    - ``build_department_judge_map()`` — build a dept->judge lookup from records
    - ``lookup_judge_for_department()`` — look up a judge name by department number

The mapping is used by the Ventura tentative rulings scraper to populate judge
names for rulings that only contain the department number.

Department numbers are normalized by stripping leading zeros for comparison
(e.g., "042" -> "42"), using the same normalization as the LA mapping module.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog
from bs4 import BeautifulSoup

from .la_dept_judges import normalize_department

logger = structlog.get_logger(__name__)

VENTURA_ASSIGNMENTS_URL = "https://www.ventura.courts.ca.gov/judicial-assignments-ventura"
JUVENILE_ASSIGNMENTS_URL = "https://www.ventura.courts.ca.gov/judicial-assignments-juvenile"
EAST_COUNTY_ASSIGNMENTS_URL = "https://www.ventura.courts.ca.gov/judicial-assignments-east-county"

ALL_ASSIGNMENT_URLS = [
    VENTURA_ASSIGNMENTS_URL,
    JUVENILE_ASSIGNMENTS_URL,
    EAST_COUNTY_ASSIGNMENTS_URL,
]

# Prefix stripped from judicial officer names
_HON_PREFIX = "Hon. "


@dataclass
class DepartmentJudge:
    """A department-to-judge entry from a Ventura judicial assignments page."""

    department: str
    judge_name: str
    title: str


def _strip_hon_prefix(name: str) -> str:
    """Strip the 'Hon. ' prefix from a judicial officer name.

    Examples:
        "Hon. Ronda J. McKaig" -> "Ronda J. McKaig"
        "Hon. Gilbert A. Romero" -> "Gilbert A. Romero"
        "Vacant" -> "Vacant"
    """
    if name.startswith(_HON_PREFIX):
        return name[len(_HON_PREFIX) :]
    return name


def parse_assignments_page_html(html: str) -> list[DepartmentJudge]:
    """Parse a Ventura judicial assignments page for dept-to-judge mappings.

    Finds the first ``<table class="tablesorter">`` with a Courtroom column
    header and extracts department, judge name, and title from each row.

    Rows with "Vacant" as the judicial officer are skipped.

    Returns an empty list if no suitable table is found.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[DepartmentJudge] = []

    # Find all tablesorter tables — the first one with a "Courtroom" header
    # is the assignments table.  (The page may have a second table for
    # appellate division assignments without a Courtroom column.)
    for table in soup.find_all("table", class_="tablesorter"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if "courtroom" not in headers:
            continue

        # Determine column indices
        courtroom_idx = headers.index("courtroom")
        officer_idx = headers.index("judicial officer") if "judicial officer" in headers else 1
        title_idx = headers.index("title") if "title" in headers else 2

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

        for row in rows:
            cells = row.find_all("td")
            if len(cells) <= max(courtroom_idx, officer_idx, title_idx):
                continue

            department = cells[courtroom_idx].get_text(strip=True)
            officer_name = cells[officer_idx].get_text(strip=True)
            title = cells[title_idx].get_text(strip=True)

            # Skip vacant positions
            if not officer_name or officer_name.lower() == "vacant":
                continue

            # Skip empty department
            if not department:
                continue

            judge_name = _strip_hon_prefix(officer_name)
            # Normalize whitespace
            judge_name = " ".join(judge_name.split())

            entries.append(
                DepartmentJudge(
                    department=department,
                    judge_name=judge_name,
                    title=title,
                )
            )

        # Only process the first table with a Courtroom header
        break

    logger.info("Parsed Ventura department-judge entries", count=len(entries))
    return entries


def build_department_judge_map(
    entries: list[DepartmentJudge],
) -> dict[str, str]:
    """Build a normalized-department -> judge-name mapping.

    Department numbers are normalized (leading zeros stripped) so lookups
    work regardless of padding.

    Args:
        entries: List of DepartmentJudge records from ``parse_assignments_page_html``.

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


def fetch_department_judge_mapping(
    timeout: float = 30.0,
) -> dict[str, str]:
    """Fetch all Ventura judicial assignment pages and return a dept->judge mapping.

    Makes HTTP GET requests to all three courthouse assignment pages (main,
    juvenile, east county), parses the tables, and builds a combined mapping.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to judge names.

    Raises:
        httpx.HTTPStatusError: If any HTTP request fails.
    """
    all_entries: list[DepartmentJudge] = []

    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        for url in ALL_ASSIGNMENT_URLS:
            logger.info("Fetching Ventura judicial assignments", url=url)
            response = client.get(url)
            response.raise_for_status()
            entries = parse_assignments_page_html(response.text)
            all_entries.extend(entries)

    dept_map = build_department_judge_map(all_entries)
    logger.info("Built Ventura department-judge mapping", departments=len(dept_map))
    return dept_map
