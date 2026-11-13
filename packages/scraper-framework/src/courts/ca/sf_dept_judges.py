"""San Francisco Superior Court -- Department-to-Judge Mapping.

Extracts department-to-judge mappings from the SF Superior Court's judicial
assignments page.

Page discovery: the page URL is year-suffixed
(``/general-information/judicial-assignments-2026`` as of 2026-11); the old
``/general-information/judicial-assignments`` URL now 301s to ``/system/403``.
The current URL is discovered from the home page nav link whose text is
"Judicial Assignments", falling back to :data:`SF_ASSIGNMENTS_URL` (#4733).

Page format: since late 2026 the assignments are inline HTML tables, one per
courthouse (Civic Center, Hall of Justice, Juvenile Justice Center, Community
Justice Center), with a department column (``DEPT`` / ``DEPARTMENT- Floor`` /
``Department``) and a judge column (``JUDGE`` / ``JUDICIAL OFFICER``). The
``<article>`` element holding the tables is what gets archived to S3.

Legacy format (fallback): before 2026-11 the page linked a department listing
PDF (``sftc-judges-deptassignment-listing-public-MMYYYY.pdf``) with columns
DEPT, JUDGE, DESIGNATION, CLERK, CTROOM, CT REPORTER, CR PHONE, BAILIFF. If the
page has no assignments table but links such a PDF, the PDF is parsed instead.
If neither is present, fetching raises ``ValueError`` naming the page URL.

Hall of Justice departments carry floor suffixes ("9—1st flr") which are
stripped. Judge names are in "LAST, First M." format and are converted to
"First M. Last".

This module provides:
    - ``parse_assignments_html()`` -- parse the assignments page HTML tables
    - ``parse_dept_listing_pdf()`` -- parse a legacy PDF into DepartmentJudge records
    - ``build_department_judge_map()`` -- build a dept->judge lookup from records
    - ``lookup_judge_for_department()`` -- look up a judge name by department number
    - ``fetch_department_judge_mapping()`` -- fetch and parse the live page
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
from bs4 import BeautifulSoup, Tag

from framework.court_directory import CourtDirectory

from .la_dept_judges import normalize_department

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)

SF_HOME_URL = "https://sf.courts.ca.gov/"

#: Last-known assignments page URL. Used only when the home page nav link
#: cannot be found; the live URL is discovered via :func:`_find_assignments_page_url`.
SF_ASSIGNMENTS_URL = "https://sf.courts.ca.gov/general-information/judicial-assignments-2026"

# Regex to extract the department number from entries like "9—1st flr", "13—2nd flr"
_DEPT_WITH_FLOOR_RE = re.compile(r"^(\d+|[A-Za-z0-9]+)\s*[—–-]\s*\d+\w+\s+flr$")

# A plausible department identifier after floor-suffix stripping: "9", "204", "405A".
_DEPT_ID_RE = re.compile(r"^[A-Za-z]?\d+[A-Za-z]?$")

# Assignments-table header cells identifying the department and judge columns.
_DEPT_HEADER_RE = re.compile(r"^dep(?:t|artment)\b", re.IGNORECASE)
_JUDGE_HEADER_RE = re.compile(r"\b(?:judge|judicial officer)\b", re.IGNORECASE)

# Nav link to the assignments page: matched on link text first, href slug second.
_ASSIGNMENTS_LINK_TEXT_RE = re.compile(r"^judicial assignments?\b", re.IGNORECASE)
_ASSIGNMENTS_HREF_RE = re.compile(r"/judicial-assignments?(?:-\d{4})?/?$", re.IGNORECASE)

# Legacy department listing PDF link text, e.g. "Judicial Roster Department Listing".
_DEPT_LISTING_TEXT_RE = re.compile(r"\bdep(?:t|artment)\.?\s+listing\b", re.IGNORECASE)

# Drupal sends requests it refuses to an access-denied page via redirect.
_ACCESS_DENIED_PATH = "/system/403"

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
    # "A.Marisa" -> "A. Marisa" (missing space after an initial).
    first_part = re.sub(r"\b([A-Za-z])\.(?=[A-Za-z])", r"\1. ", first_part.strip())
    # "Monica." -> "Monica" (stray trailing period on a full word, not an initial).
    first_part = re.sub(r"(\w{2,})\.$", r"\1", first_part)

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


def _cell_text(cell: Tag) -> str:
    """Return a table cell's text with inline markup joined and whitespace collapsed.

    Inline elements are joined without a separator so that
    ``9—1<sup>st</sup> flr`` reads "9—1st flr" and
    ``<strong>MCNAUGHTON, </strong>Michael`` reads "MCNAUGHTON, Michael".
    Block-level ``<p>`` boundaries become spaces.
    """
    parts = [p.get_text("") for p in cell.find_all("p")] or [cell.get_text("")]
    return " ".join(" ".join(parts).split())


def _header_columns(header_cells: list[Tag]) -> tuple[int, int] | None:
    """Locate the (department, judge) column indices from a table's header cells."""
    dept_idx = judge_idx = None
    for idx, cell in enumerate(header_cells):
        text = _cell_text(cell)
        if dept_idx is None and _DEPT_HEADER_RE.search(text):
            dept_idx = idx
        elif judge_idx is None and _JUDGE_HEADER_RE.search(text):
            judge_idx = idx
    if dept_idx is None or judge_idx is None:
        return None
    return dept_idx, judge_idx


def parse_assignments_html(html: str) -> list[DepartmentJudge]:
    """Parse department-to-judge entries from the SF assignments page HTML tables.

    Every ``<table>`` whose header row has a department column and a judge
    column is parsed; other tables are ignored. The header row is the
    ``<thead>`` row when present, else the table's first row. Vacant,
    visiting, and unassigned entries are skipped, and duplicate departments
    keep their first entry (e.g. a supervising judge listed before a
    second judge in the same department).

    Args:
        html: The HTML of the SF judicial assignments page (or a fragment).

    Returns:
        A list of DepartmentJudge records with normalized names.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[DepartmentJudge] = []
    seen_depts: set[str] = set()

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        thead = table.find("thead")
        header_row = thead.find("tr") if thead else rows[0]
        if header_row is None:
            continue
        columns = _header_columns(header_row.find_all(["th", "td"]))
        if columns is None:
            continue
        dept_idx, judge_idx = columns

        for row in rows:
            if row is header_row:
                continue
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(dept_idx, judge_idx):
                continue
            dept = _normalize_dept_string(_cell_text(cells[dept_idx]))
            raw_judge = _cell_text(cells[judge_idx])
            if not _DEPT_ID_RE.match(dept) or not raw_judge:
                continue
            if _is_skippable_judge(raw_judge):
                continue

            norm_dept = normalize_department(dept)
            if norm_dept in seen_depts:
                continue
            seen_depts.add(norm_dept)
            entries.append(
                DepartmentJudge(
                    department=dept,
                    judge_name=_convert_last_first_to_first_last(raw_judge),
                )
            )

    logger.info("Parsed SF department-judge entries from HTML", count=len(entries))
    return entries


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


def _find_assignments_page_url(home_html: str) -> str | None:
    """Find the judicial assignments page URL in the SF home page navigation.

    Matches on link text ("Judicial Assignments", optionally followed by a
    year) first, then on an href slug of ``judicial-assignments[-YYYY]``, so
    a renamed slug or a new year suffix is still found.

    Args:
        home_html: The HTML of the SF court home page.

    Returns:
        The absolute assignments page URL, or None if no matching link exists.
    """
    soup = BeautifulSoup(home_html, "lxml")
    links = soup.find_all("a", href=True)
    for a_tag in links:
        text = " ".join(a_tag.get_text(" ").split())
        if _ASSIGNMENTS_LINK_TEXT_RE.match(text):
            return urljoin(SF_HOME_URL, str(a_tag["href"]))
    for a_tag in links:
        href = str(a_tag["href"])
        if _ASSIGNMENTS_HREF_RE.search(href):
            return urljoin(SF_HOME_URL, href)
    return None


def _find_latest_dept_listing_url(
    assignments_html: str,
    base_url: str = SF_HOME_URL,
) -> str | None:
    """Extract the URL of the legacy department listing PDF from the assignments page.

    Matches a link whose href contains "deptassignment-listing", or a PDF
    link whose text says "Department Listing".

    Args:
        assignments_html: The HTML of the SF judicial assignments page.
        base_url: URL the page was fetched from, for resolving relative hrefs.

    Returns:
        The full URL to the department listing PDF, or None if not found.
    """
    soup = BeautifulSoup(assignments_html, "lxml")
    for a_tag in soup.find_all("a", href=True):
        href = str(a_tag["href"])
        text = " ".join(a_tag.get_text(" ").split())
        if "deptassignment-listing" in href.lower() or (
            ".pdf" in href.lower() and _DEPT_LISTING_TEXT_RE.search(text)
        ):
            return urljoin(base_url, href)
    return None


def _archivable_html(page_html: str) -> bytes:
    """Return the page's ``<article>`` element as bytes for S3 archival.

    The article holds every assignments table. Site chrome outside it
    (banners, form tokens, nav) changes independently of the assignments and
    would defeat content-hash deduplication of snapshots.
    """
    article = BeautifulSoup(page_html, "lxml").find("article")
    return str(article if article is not None else page_html).encode("utf-8")


def _discover_assignments_url(client: httpx.Client) -> str:
    """Discover the assignments page URL from the home page, or fall back."""
    try:
        home = client.get(SF_HOME_URL)
        home.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "SF home page unavailable -- using last-known assignments URL",
            error=str(exc),
            fallback_url=SF_ASSIGNMENTS_URL,
        )
        return SF_ASSIGNMENTS_URL
    url = _find_assignments_page_url(home.text)
    if url is None:
        logger.warning(
            "No 'Judicial Assignments' link on SF home page -- using last-known URL",
            fallback_url=SF_ASSIGNMENTS_URL,
        )
        return SF_ASSIGNMENTS_URL
    return url


def _fetch_and_parse_directory(
    timeout: float = 30.0,
) -> tuple[bytes, dict[str, str]]:
    """Fetch and parse the SF judicial assignments (shared helper).

    Discovers the assignments page from the home page nav, then parses its
    inline HTML tables. If the page has no assignments table but links a
    legacy department listing PDF, the PDF is downloaded and parsed instead.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (raw_bytes, dept_to_judge_mapping). ``raw_bytes`` is the
        page's ``<article>`` HTML, or the PDF bytes on the legacy path.

    Raises:
        httpx.HTTPStatusError: If the assignments page or PDF request fails.
        ValueError: If the page is access-denied, has neither an assignments
            table nor a department listing PDF link, or yields 0 departments.
    """
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        page_url = _discover_assignments_url(client)
        logger.info("Fetching SF judicial assignments page", url=page_url)
        page_response = client.get(page_url)
        page_response.raise_for_status()

        final_url = str(page_response.url)
        if _ACCESS_DENIED_PATH in final_url:
            msg = (
                f"SF judicial assignments page {page_url} redirected to {final_url} "
                "(access denied) -- the page has moved; update SF_ASSIGNMENTS_URL "
                "or the home page link discovery in sf_dept_judges.py"
            )
            raise ValueError(msg)

        entries = parse_assignments_html(page_response.text)
        if entries:
            raw = _archivable_html(page_response.text)
            source = final_url
        else:
            pdf_url = _find_latest_dept_listing_url(page_response.text, final_url)
            if not pdf_url:
                msg = (
                    f"SF judicial assignments page {final_url} has neither an assignments "
                    "table (DEPT/JUDGE header columns) nor a department listing PDF link "
                    "-- the page layout changed; update sf_dept_judges.py"
                )
                raise ValueError(msg)
            logger.info("Downloading SF department listing PDF", url=pdf_url)
            pdf_response = client.get(pdf_url)
            pdf_response.raise_for_status()
            raw = pdf_response.content
            entries = parse_dept_listing_pdf(raw)
            source = pdf_url

    dept_map = build_department_judge_map(entries)
    if not dept_map:
        msg = f"Parsed 0 departments from SF judicial assignments source {source}"
        raise ValueError(msg)
    logger.info("Built SF department-judge mapping", departments=len(dept_map), source=source)
    return raw, dept_map


def fetch_department_judge_mapping(
    timeout: float = 30.0,
) -> dict[str, str]:
    """Fetch the SF judicial assignments and return a dept->judge mapping.

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

    Implements ``CourtDirectory.fetch_current()`` by parsing the assignments
    tables on the SF judicial assignments page (or the legacy department
    listing PDF it may link instead). The base class handles S3 archival, DB
    storage, and content-hash deduplication.

    Overrides ``save_snapshot()`` so legacy PDF snapshots use
    ``application/pdf`` content type and a ``.pdf`` S3 key extension.

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
        """Fetch the live SF judicial assignments.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_bytes, dept_to_judge_mapping); see
            :func:`_fetch_and_parse_directory`.
        """
        return _fetch_and_parse_directory(self._timeout)

    def save_snapshot(
        self,
        raw: bytes,
        mapping: dict[str, str],
        court_id: str,
    ) -> bool:
        """Archive a directory snapshot to S3 and DB.

        HTML snapshots (the current format) go through the base class
        unchanged. Legacy PDF snapshots (``raw`` starts with ``%PDF``) use
        ``application/pdf`` content type and a ``.pdf`` S3 key extension.

        Parameters
        ----------
        raw : bytes
            The raw ``<article>`` HTML or PDF content.
        mapping : dict[str, str]
            The parsed {department: judge_name} mapping.
        court_id : str
            The court identifier.

        Returns
        -------
        bool
            True if a new snapshot was inserted, False if deduplicated.
        """
        if not raw.startswith(b"%PDF"):
            return super().save_snapshot(raw, mapping, court_id)

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
