"""Riverside County Superior Court — Department-to-Judge Mapping.

Scrapes the Riverside tentative rulings index page at:
    https://www.riverside.courts.ca.gov/online-services/tentative-rulings

The page contains PDF links with text in the format:
    "Department PS1 - Honorable Arthur Hester III"

This module provides:
    - ``fetch_department_judge_mapping()`` — scrape the live page and return the mapping
    - ``parse_index_page_html()`` — parse HTML link text into DepartmentJudge records
    - ``build_department_judge_map()`` — build a dept→judge lookup from records
    - ``lookup_judge_for_department()`` — look up a judge name by department number

The mapping is used by the Riverside tentative rulings scraper to populate judge
names for rulings that don't include the judge's name in the PDF content.

Department numbers are normalized by stripping leading zeros for comparison
(e.g., "01" → "1"), using the same normalization as the LA mapping module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
import structlog
from bs4 import BeautifulSoup

from .la_dept_judges import normalize_department

logger = structlog.get_logger(__name__)

INDEX_URL = "https://www.riverside.courts.ca.gov/online-services/tentative-rulings"

# Link text: "Department PS1 - Honorable Arthur Hester III"
_LINK_TEXT_RE = re.compile(
    r"Department\s+(?P<department>\S+)\s*-\s*Honorable\s+(?P<judge_name>.+)",
    re.IGNORECASE,
)


@dataclass
class DepartmentJudge:
    """A department-to-judge entry from the Riverside tentative rulings page."""

    department: str
    judge_name: str


def parse_index_page_html(html: str) -> list[DepartmentJudge]:
    """Parse the Riverside tentative rulings index page for dept-to-judge mappings.

    Finds all PDF links whose link text matches the pattern
    "Department {CODE} - Honorable {Name}" and extracts the department code
    and judge name.

    Returns an empty list if no matching links are found.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[DepartmentJudge] = []
    seen_depts: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if ".pdf" not in href.lower():
            continue

        # Normalize non-breaking spaces in link text
        link_text = a_tag.get_text(separator=" ", strip=True).replace("\xa0", " ")
        m = _LINK_TEXT_RE.search(link_text)
        if not m:
            continue

        department = m.group("department").strip()
        raw_name = m.group("judge_name").strip()
        # Normalize whitespace in judge name
        judge_name = " ".join(raw_name.split())

        # Deduplicate by department (keep the first occurrence, which is the
        # most recent ruling link on the page)
        norm_dept = normalize_department(department)
        if norm_dept in seen_depts:
            logger.debug(
                "Duplicate department link — keeping first",
                department=norm_dept,
                judge_name=judge_name,
            )
            continue
        seen_depts.add(norm_dept)

        entries.append(DepartmentJudge(department=department, judge_name=judge_name))

    logger.info("Parsed Riverside department-judge entries", count=len(entries))
    return entries


def build_department_judge_map(
    entries: list[DepartmentJudge],
) -> dict[str, str]:
    """Build a normalized-department → judge-name mapping.

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
    """Fetch the Riverside tentative rulings page and return a dept→judge mapping.

    Makes a single HTTP GET to the tentative rulings index page, parses the
    PDF link text for department-to-judge entries, and builds the mapping.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to judge names.

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    logger.info("Fetching Riverside department-judge mapping", url=INDEX_URL)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        verify=False,  # Riverside has a bad TLS cert
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        response = client.get(INDEX_URL)
        response.raise_for_status()

    entries = parse_index_page_html(response.text)
    dept_map = build_department_judge_map(entries)
    logger.info("Built Riverside department-judge mapping", departments=len(dept_map))
    return dept_map
