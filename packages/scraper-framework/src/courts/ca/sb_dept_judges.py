"""San Bernardino County Superior Court -- Department-to-Judge Mapping (PDF Extraction).

Extracts department-to-judge mappings from the San Bernardino "Schedule of
Assignments" PDF, available at:
    https://sanbernardino.courts.ca.gov/system/files/general/schedassign.pdf

The PDF is 170+ pages. Most pages are detailed per-department calendars.
The relevant data is on "CALENDAR OF ASSIGNMENTS" summary pages (one per
courthouse district), which list departments and their assigned judges in
a compact format::

    DEPARTMENT B1   COMMISSIONER JAMES R. BAXTER
    DEPARTMENT B2   COMMISSIONER JASON S. WILKINSON
                    JUDGE ALBERT HSUEH
    DEPARTMENT B4
                    SUPERVISING JUDGE

Districts include: Barstow (B), Big Bear (M), Fontana (F), Joshua Tree (M),
Juvenile (J), Needles (N), Rancho Cucamonga (R), San Bernardino Justice Center
(S), SB Family Law (S43+), and Victorville (V).

This module provides:
    - ``parse_schedule_pdf()`` -- parse the PDF into DepartmentJudge records
    - ``build_department_judge_map()`` -- build a dept->judge lookup from records
    - ``lookup_judge_for_department()`` -- look up a judge name by department number
    - ``fetch_department_judge_mapping()`` -- fetch and parse the live PDF
    - ``SanBernardinoCourtDirectory`` -- CourtDirectory subclass with S3 archival
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING

import httpx
import pdfplumber
import structlog

from framework.court_directory import CourtDirectory

from .la_dept_judges import normalize_department

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)

SB_SCHEDULE_URL = "https://sanbernardino.courts.ca.gov/system/files/general/schedassign.pdf"

# Regex to match "DEPARTMENT <code>" lines
_DEPT_RE = re.compile(
    r"DEPARTMENT\s+([A-Za-z0-9/]+(?:\s*/\s*[A-Za-z0-9-]+)?)",
    re.IGNORECASE,
)

# Regex to match "JUDGE <name>" or "COMMISSIONER <name>" lines
_JUDGE_RE = re.compile(
    r"(?:JUDGE|COMMISSIONER)\s+(.+)",
    re.IGNORECASE,
)

# Title suffixes to strip from judge names (kept for reference but not in name)
_TITLE_SUFFIXES = {
    "SUPERVISING JUDGE",
    "ASSISTANT PRESIDING JUDGE",
    "PRESIDING JUDGE",
    "ASSIGNED JUDGE",
    "JUVENILE PRESIDING JUDGE",
    "ASST SUPERVISING JUDGE",
    "ASST. SUPERVISING JUDGE",
    "SUPERVISING CRIMINAL JUDGE",
}

# Skip these entries entirely
_SKIP_PATTERNS = {"VACANT", "MEDIATION ROOM"}


@dataclass
class DepartmentJudge:
    """A department-to-judge entry from the SB schedule of assignments PDF."""

    department: str
    judge_name: str


def _is_calendar_page(text: str) -> bool:
    """Check if a page is a CALENDAR OF ASSIGNMENTS summary page.

    These are the compact summary pages listing all departments in a district.
    They contain the header "CALENDAR OF ASSIGNMENTS" and list departments
    with their judges.

    Args:
        text: The extracted text of a PDF page.

    Returns:
        True if this is a summary page.
    """
    return "CALENDAR OF ASSIGNMENTS" in text


def _clean_judge_name(raw_name: str) -> str:
    """Clean and normalize a judge name from the SB PDF.

    Removes title suffixes like "SUPERVISING JUDGE" and normalizes casing.

    Args:
        raw_name: The raw name text after "JUDGE" or "COMMISSIONER".

    Returns:
        The cleaned judge name in title case.
    """
    name = raw_name.strip()

    # Remove title suffixes (case-insensitive)
    for suffix in _TITLE_SUFFIXES:
        # Check if the entire remaining text is just a title suffix
        if name.upper() == suffix:
            return ""
        # Check if name ends with a title suffix after the actual name
        upper_name = name.upper()
        if upper_name.endswith(suffix):
            name = name[: -len(suffix)].strip()
            break

    # Normalize whitespace
    name = " ".join(name.split())

    if not name:
        return ""

    # Title-case the name
    return _title_case_judge_name(name)


def _title_case_judge_name(name: str) -> str:
    """Title-case a judge name, preserving common patterns.

    Handles Roman numerals (II, III, IV), initials with dots,
    Mc/Mac prefixes, and multi-word names.

    Examples:
        "JAMES R. BAXTER"        -> "James R. Baxter"
        "WM. JEFFERSON POWELL, IV" -> "Wm. Jefferson Powell, IV"
        "J. BRUCE MINTON"        -> "J. Bruce Minton"
        "ARTHUR B. BENNER II"    -> "Arthur B. Benner II"

    Args:
        name: A judge name in upper case.

    Returns:
        The title-cased name.
    """
    # Roman numeral suffixes to preserve
    roman_numerals = {"I", "II", "III", "IV", "V", "VI"}

    words = name.split()
    result: list[str] = []
    for word in words:
        # Preserve commas attached to words
        trailing_comma = ""
        if word.endswith(","):
            trailing_comma = ","
            word = word[:-1]

        upper_word = word.upper()

        # Preserve Roman numerals
        if upper_word in roman_numerals:
            result.append(upper_word + trailing_comma)
            continue

        # Handle initials like "J." or "R."
        if len(word) == 2 and word[1] == "." and word[0].isalpha():
            result.append(word[0].upper() + "." + trailing_comma)
            continue

        # Handle abbreviated first names like "WM."
        if word.endswith(".") and len(word) <= 4:
            result.append(word.capitalize() + trailing_comma)
            continue

        # Standard capitalization (handle hyphenated names like "Garcia-Rodrigo")
        if "-" in word:
            parts = word.split("-")
            capitalized = "-".join(p.capitalize() for p in parts)
            result.append(capitalized + trailing_comma)
            continue
        result.append(word.capitalize() + trailing_comma)

    return " ".join(result)


def _parse_calendar_page_lines(lines: list[str]) -> list[tuple[str, str]]:
    """Parse lines from a CALENDAR OF ASSIGNMENTS page into (dept, judge) pairs.

    Handles three layout patterns found in the SB PDF:

    1. Inline: ``DEPARTMENT R1 JUDGE JAMES J. HOSKING``
    2. Judge on next line: ``DEPARTMENT S6`` followed by ``JUDGE KYLE S. BRODIE``
    3. Judge on previous line: ``JUDGE JOEL S. AGRON`` followed by ``DEPARTMENT S1``

    Pattern 3 occurs when the judge name appears above its department line
    (e.g., SB Justice Center page).

    Args:
        lines: Text lines from a single CALENDAR OF ASSIGNMENTS page.

    Returns:
        A list of (department_code, judge_name) tuples.
    """
    pairs: list[tuple[str, str]] = []
    pending_judge: str | None = None
    current_dept: str | None = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Skip page footers
        if line.startswith("Page ") or line.startswith("Effective:"):
            continue

        # Try to match a DEPARTMENT line
        dept_match = _DEPT_RE.match(line)
        if dept_match:
            dept_code = dept_match.group(1).strip()

            # Check if the rest of the line has a judge name (Pattern 1: inline)
            rest = line[dept_match.end() :].strip()
            judge_match = _JUDGE_RE.match(rest)
            if judge_match:
                raw_name = judge_match.group(1).strip()
                judge_name = _clean_judge_name(raw_name)
                if judge_name and judge_name.upper() not in _SKIP_PATTERNS:
                    pairs.append((dept_code, judge_name))
                pending_judge = None
                current_dept = None
            elif pending_judge:
                # Pattern 3: judge appeared on the previous line
                pairs.append((dept_code, pending_judge))
                pending_judge = None
                current_dept = None
            else:
                # Pattern 2: department alone; judge may follow on next line
                current_dept = dept_code
                pending_judge = None
            continue

        # Try to match a standalone JUDGE/COMMISSIONER line
        judge_match = _JUDGE_RE.match(line)
        if judge_match:
            raw_name = judge_match.group(1).strip()
            judge_name = _clean_judge_name(raw_name)
            if not judge_name or judge_name.upper() in _SKIP_PATTERNS:
                continue

            if current_dept:
                # Pattern 2: this is the judge for the preceding department
                pairs.append((current_dept, judge_name))
                current_dept = None
                pending_judge = None
            else:
                # Pattern 3: judge before its department (on next line)
                pending_judge = judge_name
            continue

        # Lines that are title suffixes (e.g., "SUPERVISING JUDGE") —
        # these follow a DEPARTMENT line and should not clear the department
        upper_line = line.upper().strip()
        if upper_line in _TITLE_SUFFIXES:
            continue

        # Unrecognized line — reset pending state
        pending_judge = None
        current_dept = None

    return pairs


def parse_schedule_pdf(pdf_bytes: bytes) -> list[DepartmentJudge]:
    """Parse the SB schedule of assignments PDF for dept-to-judge mappings.

    Only processes "CALENDAR OF ASSIGNMENTS" summary pages, which list
    all departments and their assigned judges for each courthouse district.

    Args:
        pdf_bytes: The raw PDF file content.

    Returns:
        A list of DepartmentJudge records.
    """
    entries: list[DepartmentJudge] = []
    seen_depts: set[str] = set()

    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if not text or not _is_calendar_page(text):
                    continue

                lines = text.split("\n")
                pairs = _parse_calendar_page_lines(lines)

                for dept_code, judge_name in pairs:
                    norm = normalize_department(dept_code)
                    if norm not in seen_depts:
                        seen_depts.add(norm)
                        entries.append(
                            DepartmentJudge(
                                department=dept_code,
                                judge_name=judge_name,
                            )
                        )
                    else:
                        logger.debug(
                            "Duplicate department -- keeping first",
                            department=norm,
                            judge_name=judge_name,
                        )
    except Exception:
        logger.error("Failed to parse SB schedule PDF", exc_info=True)
        return []

    logger.info("Parsed SB department-judge entries", count=len(entries))
    return entries


def build_department_judge_map(
    entries: list[DepartmentJudge],
) -> dict[str, str]:
    """Build a normalized-department -> judge-name mapping.

    Department numbers are normalized (leading zeros stripped) so lookups
    work regardless of padding.

    Args:
        entries: List of DepartmentJudge records from ``parse_schedule_pdf``.

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


def _fetch_and_parse_directory(
    timeout: float = 30.0,
) -> tuple[bytes, dict[str, str]]:
    """Fetch and parse the SB schedule of assignments PDF (shared helper).

    Downloads the PDF from the stable URL and parses the summary pages.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (raw_pdf_bytes, dept_to_judge_mapping).

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    logger.info("Fetching SB schedule of assignments PDF", url=SB_SCHEDULE_URL)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        response = client.get(SB_SCHEDULE_URL)
        response.raise_for_status()

    raw = response.content
    entries = parse_schedule_pdf(raw)
    dept_map = build_department_judge_map(entries)
    logger.info("Built SB department-judge mapping", departments=len(dept_map))
    return raw, dept_map


def fetch_department_judge_mapping(
    timeout: float = 30.0,
) -> dict[str, str]:
    """Fetch the SB schedule PDF and return a dept->judge mapping.

    Downloads the PDF from the stable URL and parses the CALENDAR OF
    ASSIGNMENTS summary pages.

    This is a convenience function that does **not** perform snapshotting.
    For production use with archival, use :class:`SanBernardinoCourtDirectory` instead.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to judge names.

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
    """
    _raw, dept_map = _fetch_and_parse_directory(timeout)
    return dept_map


class SanBernardinoCourtDirectory(CourtDirectory):
    """San Bernardino County Superior Court department-to-judge directory with snapshotting.

    Implements ``CourtDirectory.fetch_current()`` by downloading the schedule of
    assignments PDF and parsing the CALENDAR OF ASSIGNMENTS summary pages.
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
    COURT_ID: str = "ca_san_bernardino"

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
        """Fetch the live SB schedule of assignments PDF.

        Downloads the PDF and parses the CALENDAR OF ASSIGNMENTS summary pages.

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
        :attr:`COURT_ID` (``"ca_san_bernardino"``).

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
