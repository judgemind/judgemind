"""San Francisco Superior Court -- Department-to-Judge Mapping (PDF Extraction).

Extracts department-to-judge mappings from the SF Superior Court's
department listing PDF, available at:
    https://sf.courts.ca.gov/general-information/judicial-assignments

The court publishes two PDFs:
  - Department Listing (primary): ``sftc-judges-deptassignment-listing-public-MMYYYY.pdf``
  - Alphabetical Roster (supplementary): ``sftc-judges-roster-alpha-public-MMYYYY.pdf``

This module parses the department listing PDF, which contains a table with columns:
    DEPT, JUDGE, DESIGNATION, CLERK, CTROOM, CT REPORTER, CR PHONE, BAILIFF

The department listing spans two pages:
  - Page 1: Civic Center Courthouse (depts 204-624)
  - Page 2: Hall of Justice (depts 9-30) + Juvenile Justice Center (depts 1-8)

Judge names are in "LAST, First M." format and are converted to "First M. Last".

This module provides:
    - ``parse_dept_listing_pdf()`` -- parse a PDF file into DepartmentJudge records
    - ``build_department_judge_map()`` -- build a dept->judge lookup from records
    - ``lookup_judge_for_department()`` -- look up a judge name by department number
    - ``fetch_department_judge_mapping()`` -- fetch and parse the live PDF
    - ``SFCourtDirectory`` -- CourtDirectory subclass with S3 archival

The mapping is used to populate judge names for SF rulings that only contain
the department number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import httpx
import pdfplumber
import structlog
from bs4 import BeautifulSoup

from framework.court_directory import CourtDirectory

from .la_dept_judges import normalize_department

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)

SF_ASSIGNMENTS_URL = "https://sf.courts.ca.gov/general-information/judicial-assignments"

# Regex to extract the department number from entries like "9—1st flr", "13—2nd flr"
_DEPT_WITH_FLOOR_RE = re.compile(r"^(\d+|[A-Za-z0-9]+)\s*[—–-]\s*\d+\w+\s+flr$")

# Entries to skip (not actual judge assignments)
_SKIP_JUDGE_PATTERNS = {
    "visiting judge",
    "visting judge",  # typo in actual PDF
    "visting judge/ jpt",
    "unassigned",
    "vacant",
    "commissioner",
    "civil case management",
    "judge pro tem/ vj",
}


@dataclass
class DepartmentJudge:
    """A department-to-judge entry from the SF department listing PDF."""

    department: str
    judge_name: str


def _normalize_dept_string(raw_dept: str) -> str:
    """Extract the department number from a raw department cell value.

    Handles entries like "9—1st flr" (Hall of Justice floor suffixes)
    and plain numbers like "204".

    Args:
        raw_dept: The raw department string from the PDF table cell.

    Returns:
        The cleaned department identifier (e.g., "9", "204", "405A").
    """
    raw_dept = raw_dept.strip()
    m = _DEPT_WITH_FLOOR_RE.match(raw_dept)
    if m:
        return m.group(1)
    return raw_dept


def _convert_last_first_to_first_last(name: str) -> str:
    """Convert "LAST, First M." format to "First M. Last".

    Handles parenthetical annotations like "(PJ)", "(APJ)",
    "(also JJC, D2)" by stripping them.

    Examples:
        "MOODY, Ross C."           -> "Ross C. Moody"
        "EAST, Rochelle (PJ)"      -> "Rochelle East"
        "HITE, Christopher C. (APJ)" -> "Christopher C. Hite"
        "CHAN, Roger (also JJC, D2)" -> "Roger Chan"
        "CHUN, A. Marisa"          -> "A. Marisa Chun"
        "VAN AKEN, Christine"      -> "Christine Van Aken"

    Args:
        name: A judge name in "LAST, First M." format.

    Returns:
        The judge name in "First M. Last" format, title-cased.
    """
    # Strip parenthetical annotations
    name = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()

    if "," not in name:
        # Not in LAST, First format -- return as-is with normalized case
        return " ".join(name.split()).title()

    last_part, first_part = name.split(",", 1)
    last_part = last_part.strip()
    first_part = first_part.strip()

    # Title-case the last name (handles "VAN AKEN" -> "Van Aken", "MCNAUGHTON" -> "Mcnaughton")
    # We use a smarter title-casing that handles common prefixes
    last_name = _title_case_name(last_part)
    first_name = _title_case_name(first_part)

    # Normalize whitespace
    full_name = f"{first_name} {last_name}"
    return " ".join(full_name.split())


def _title_case_name(name: str) -> str:
    """Title-case a name, handling common prefixes and suffixes.

    Examples:
        "MOODY"     -> "Moody"
        "VAN AKEN"  -> "Van Aken"
        "MCNAUGHTON" -> "McNaughton"

    Args:
        name: A name part to title-case.

    Returns:
        The title-cased name.
    """
    # Handle Mc/Mac prefixes
    upper = name.upper()
    if upper.startswith("MC") and len(name) > 2:
        return "Mc" + name[2:].title()
    if upper.startswith("MAC") and len(name) > 3 and name[3:4].isalpha():
        return "Mac" + name[3:].title()

    # Standard title case for multi-word names
    return " ".join(word.capitalize() for word in name.split())


def _is_skippable_judge(judge_text: str) -> bool:
    """Check if a judge cell value should be skipped.

    Skips visiting judges, vacancies, unassigned positions,
    commissioners (by title only, not named commissioners),
    and administrative entries.

    Args:
        judge_text: The raw judge name from the PDF table.

    Returns:
        True if this entry should be excluded from the mapping.
    """
    normalized = judge_text.strip().lower()
    # Check exact matches and prefix matches
    if normalized in _SKIP_JUDGE_PATTERNS:
        return True
    # Also skip entries starting with "Visiting" or "Visting"
    if normalized.startswith("visiting") or normalized.startswith("visting"):
        return True
    return False


def parse_dept_listing_pdf(pdf_bytes: bytes) -> list[DepartmentJudge]:
    """Parse the SF department listing PDF for dept-to-judge mappings.

    Opens the PDF from bytes, extracts tables from all pages, and
    returns a list of DepartmentJudge records. Skips vacant, visiting,
    and unassigned entries.

    Args:
        pdf_bytes: The raw PDF file content.

    Returns:
        A list of DepartmentJudge records with normalized names.
    """
    entries: list[DepartmentJudge] = []
    seen_depts: set[str] = set()

    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                if not tables:
                    continue

                for table in tables:
                    for row in table:
                        if not row or len(row) < 2:
                            continue

                        raw_dept = (row[0] or "").strip()
                        raw_judge = (row[1] or "").strip()

                        # Skip header rows and empty entries
                        if not raw_dept or not raw_judge:
                            continue
                        if raw_dept.upper() in ("DEPT", ""):
                            continue

                        # Skip non-judge entries
                        if _is_skippable_judge(raw_judge):
                            continue

                        dept = _normalize_dept_string(raw_dept)
                        judge_name = _convert_last_first_to_first_last(raw_judge)

                        # Normalize whitespace in judge name
                        judge_name = " ".join(judge_name.split())

                        # Deduplicate by normalized department (keep first occurrence)
                        norm_dept = normalize_department(dept)
                        if norm_dept in seen_depts:
                            logger.debug(
                                "Duplicate department -- keeping first",
                                department=norm_dept,
                                judge_name=judge_name,
                            )
                            continue
                        seen_depts.add(norm_dept)

                        entries.append(
                            DepartmentJudge(
                                department=dept,
                                judge_name=judge_name,
                            )
                        )
    except Exception:
        logger.error("Failed to parse SF department listing PDF", exc_info=True)
        return []

    logger.info("Parsed SF department-judge entries", count=len(entries))
    return entries


def build_department_judge_map(
    entries: list[DepartmentJudge],
) -> dict[str, str]:
    """Build a normalized-department -> judge-name mapping.

    Department numbers are normalized (leading zeros stripped) so lookups
    work regardless of padding.

    Args:
        entries: List of DepartmentJudge records from ``parse_dept_listing_pdf``.

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
                "Duplicate department -- keeping first entry",
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


def _find_latest_dept_listing_url(
    assignments_html: str,
) -> str | None:
    """Extract the URL of the latest department listing PDF from the assignments page.

    The SF judicial assignments page links to multiple PDFs. We want the
    one with "deptassignment-listing" in the filename.

    Args:
        assignments_html: The HTML of the SF judicial assignments page.

    Returns:
        The full URL to the department listing PDF, or None if not found.
    """
    soup = BeautifulSoup(assignments_html, "lxml")
    for a_tag in soup.find_all("a", href=True):
        href = str(a_tag["href"])
        if "deptassignment-listing" in href.lower():
            return urljoin(SF_ASSIGNMENTS_URL, href)
    return None


def _fetch_and_parse_directory(
    timeout: float = 30.0,
) -> tuple[bytes, dict[str, str]]:
    """Fetch and parse the SF department listing PDF (shared helper).

    First fetches the judicial assignments page to find the latest PDF link,
    then downloads and parses the PDF.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (raw_pdf_bytes, dept_to_judge_mapping).

    Raises:
        httpx.HTTPStatusError: If any HTTP request fails.
        ValueError: If the department listing PDF link cannot be found.
    """
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        # Step 1: Fetch the assignments page to find the latest PDF link
        logger.info("Fetching SF judicial assignments page", url=SF_ASSIGNMENTS_URL)
        page_response = client.get(SF_ASSIGNMENTS_URL)
        page_response.raise_for_status()

        pdf_url = _find_latest_dept_listing_url(page_response.text)
        if not pdf_url:
            msg = "Could not find department listing PDF link on SF judicial assignments page"
            raise ValueError(msg)

        # Step 2: Download the PDF
        logger.info("Downloading SF department listing PDF", url=pdf_url)
        pdf_response = client.get(pdf_url)
        pdf_response.raise_for_status()

    raw = pdf_response.content
    entries = parse_dept_listing_pdf(raw)
    dept_map = build_department_judge_map(entries)
    logger.info("Built SF department-judge mapping", departments=len(dept_map))
    return raw, dept_map


def fetch_department_judge_mapping(
    timeout: float = 30.0,
) -> dict[str, str]:
    """Fetch the SF department listing PDF and return a dept->judge mapping.

    First fetches the judicial assignments page to discover the latest PDF URL,
    then downloads and parses the PDF.

    This is a convenience function that does **not** perform snapshotting.
    For production use with archival, use :class:`SFCourtDirectory` instead.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to judge names.

    Raises:
        httpx.HTTPStatusError: If any HTTP request fails.
        ValueError: If the PDF link cannot be found.
    """
    _raw, dept_map = _fetch_and_parse_directory(timeout)
    return dept_map


class SFCourtDirectory(CourtDirectory):
    """San Francisco Superior Court department-to-judge directory with snapshotting.

    Implements ``CourtDirectory.fetch_current()`` by downloading the department
    listing PDF from the SF judicial assignments page and parsing the table.
    The base class handles S3 archival, DB storage, and content-hash deduplication.

    Overrides ``save_snapshot()`` to use ``application/pdf`` content type and
    ``.pdf`` S3 key extension instead of the base class defaults.

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
    COURT_ID: str = "ca_san_francisco"

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
        """Fetch the live SF department listing PDF.

        Downloads the latest department listing PDF from the judicial assignments
        page and parses department-to-judge mappings.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_pdf_bytes, dept_to_judge_mapping).
        """
        return _fetch_and_parse_directory(self._timeout)

    def save_snapshot(
        self,
        raw: bytes,
        mapping: dict[str, str],
        court_id: str,
    ) -> bool:
        """Archive a PDF directory snapshot to S3 and DB.

        Overrides the base class to use ``application/pdf`` content type and
        ``.pdf`` S3 key extension.

        Parameters
        ----------
        raw : bytes
            The raw PDF content.
        mapping : dict[str, str]
            The parsed {department: judge_name} mapping.
        court_id : str
            The court identifier.

        Returns
        -------
        bool
            True if a new snapshot was inserted, False if deduplicated.
        """
        import json
        from datetime import UTC, datetime

        from framework.hashing import sha256_hex

        content_hash = sha256_hex(raw)
        now = datetime.now(UTC)
        s3_key = f"directories/{court_id}/{now.strftime('%Y%m%dT%H%M%SZ')}.pdf"

        self._s3.put_object(
            Bucket=self._bucket,
            Key=s3_key,
            Body=raw,
            ContentType="application/pdf",
            Metadata={
                "court-id": court_id,
                "content-hash": content_hash,
                "captured-at": now.isoformat(),
            },
        )
        logger.info(
            "Archived directory PDF to S3",
            court_id=court_id,
            s3_key=s3_key,
            size=len(raw),
        )

        if self._is_duplicate(court_id, content_hash):
            logger.info(
                "Directory unchanged -- skipping DB insert",
                court_id=court_id,
                content_hash=content_hash[:12],
            )
            return False

        mapping_json = json.dumps(mapping, sort_keys=True)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO court_directory_snapshots
                    (court_id, captured_at, s3_key, mapping, content_hash)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (court_id, now, s3_key, mapping_json, content_hash),
            )
        self._conn.commit()

        logger.info(
            "Saved directory snapshot",
            court_id=court_id,
            departments=len(mapping),
            content_hash=content_hash[:12],
        )
        return True

    def fetch_and_snapshot(self, court_id: str | None = None) -> dict[str, str]:
        """Fetch the live directory and save a snapshot.

        Overrides the base class to default ``court_id`` to
        :attr:`COURT_ID` (``"ca_san_francisco"``).

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
