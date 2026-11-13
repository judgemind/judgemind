"""LA County Superior Court — Department-to-Judge Mapping.

Live source (since 2026-11, #4874): the weekly "Judicial Assignment and
Directory" PDF at :data:`JUDICIAL_DIRECTORY_PDF_URL` (plain ``http`` on port 90;
the scraper security group has a port-90 egress rule for it). The PDF is
grouped by region (``CENTRAL``) and courthouse (``Stanley Mosk (VOIP: 55)``);
each courthouse block has the columns::

    Dept | Rm | Phone | Judicial Officer | Primary Assignment | Litigation Area(s)

Officer cells read "Judge William H. Forman" or "Commissioner Naomi R. Cohen",
sometimes with a role suffix such as ``(SJ)``, ``(ASJ)`` or a note that wraps
onto the next line (``(SJ-`` / ``Probate/Mental Health)``). Long names can wrap
too ("LaTonya Hadnot" / "Prioleau"). Wrapped lines are joined before the title
and parenthetical suffixes are stripped. The trailing alphabetical index and
location legend pages are not parsed.

Legacy source (retired): the judicial officer search page at
    https://www.lacourt.ca.gov/judicialofficers/ui/SearchResult.aspx
has returned IIS 503 since 2026-07 (the site moved to a new SPA). Its HTML
parser is kept so archived S3 snapshots can still be re-parsed. That page had a
sortable HTML table (class ``joresultstable``) with columns:
    Name, Title, Location, Dept, Phone, Primary Assignment, Litigation Areas

The Name column contains "Last, First" in a single cell.  An older version of
the page used a table with ``id="GridView1"`` and separate Last Name / First
Name columns — the parser supports both formats for backward compatibility with
archived snapshots.

This module provides:
    - ``fetch_department_judge_mapping()`` — fetch the live PDF and return the mapping
    - ``parse_judicial_directory_pdf()`` — parse the weekly PDF into DirectoryOfficer records
    - ``parse_judicial_officers_html()`` — parse archived HTML into JudicialOfficer records
    - ``build_department_judge_map()`` — build a dept→judge lookup from officer records
    - ``lookup_judge_for_department()`` — look up a judge name by department number

The mapping is used by the LA tentative rulings scraper to populate judge names
for rulings that don't include the judge's name in the PDF/HTML content.

Department numbers are normalized by stripping leading zeros for comparison
(e.g., "052" → "52", "005" → "5"), since the tentative rulings dropdown
uses unpadded numbers while the directory uses zero-padded numbers.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any

import httpx
import pdfplumber
import structlog
from bs4 import BeautifulSoup

from framework.court_directory import CourtDirectory

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)

#: Weekly "Judicial Assignment and Directory" PDF — the live roster source (#4874).
JUDICIAL_DIRECTORY_PDF_URL = (
    "http://mycourtservices.lacourt.org:90/lineupreports/judicial%20directory.pdf"
)

#: Retired legacy judicial officer search page (IIS 503 since 2026-07). Kept for reference;
#: archived snapshots of it are still parsed by :func:`parse_judicial_officers_html`.
JUDICIAL_OFFICERS_URL = "https://www.lacourt.ca.gov/judicialofficers/ui/SearchResult.aspx"

#: Primary assignment ranked first when a department id collides across courthouses.
CIVIL_ASSIGNMENT = "Civil"

# CSS class on the current (2025+) judicial officers table
TABLE_CLASS = "joresultstable"

# Legacy table id used before the 2025 redesign (kept for archived snapshots)
LEGACY_TABLE_ID = "GridView1"


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


def _parse_combined_name(name_text: str) -> tuple[str, str]:
    """Split a combined "Last, First" name into (last_name, first_name).

    If there is no comma, the entire string is treated as the last name
    and the first name is empty.

    Examples:
        "Abeles, Jerrold" → ("Abeles", "Jerrold")
        "Duffy-Lewis, Kerry" → ("Duffy-Lewis", "Kerry")
        "Smith" → ("Smith", "")
    """
    if "," in name_text:
        last, first = name_text.split(",", 1)
        return last.strip(), first.strip()
    return name_text.strip(), ""


def _parse_new_format(table: object) -> list[JudicialOfficer]:
    """Parse the current (2025+) table format with class ``joresultstable``.

    Columns: Name, Title, Location, Dept, Phone, Primary Assignment, Litigation Areas.
    The table has a ``<thead>`` but no ``<tbody>``; data rows are direct ``<tr>``
    children of the table.
    """
    officers: list[JudicialOfficer] = []
    # Skip header rows inside <thead>
    thead = table.find("thead")  # type: ignore[union-attr]
    header_rows = set()
    if thead:
        for tr in thead.find_all("tr"):
            header_rows.add(id(tr))

    for row in table.find_all("tr"):  # type: ignore[union-attr]
        if id(row) in header_rows:
            continue
        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        last_name, first_name = _parse_combined_name(cells[0].get_text(strip=True))
        officer = JudicialOfficer(
            last_name=last_name,
            first_name=first_name,
            title=cells[1].get_text(strip=True),
            courthouse=cells[2].get_text(strip=True),
            department=cells[3].get_text(strip=True),
            phone=cells[4].get_text(strip=True),
            primary_assignment=cells[5].get_text(strip=True),
        )
        officers.append(officer)
    return officers


def _parse_legacy_format(table: object) -> list[JudicialOfficer]:
    """Parse the legacy (pre-2025) table format with id ``GridView1``.

    Columns: Last Name, First Name, Title, Courthouse, Department, Phone,
    Primary Assignment.  The table may have a ``<tbody>`` wrapping data rows.
    """
    officers: list[JudicialOfficer] = []
    tbody = table.find("tbody")  # type: ignore[union-attr]
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]  # type: ignore[union-attr]

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
    return officers


def parse_judicial_officers_html(html: str) -> list[JudicialOfficer]:
    """Parse the judicial officers HTML page into a list of JudicialOfficer records.

    Supports two table formats:

    **Current (2025+):** ``<table class="joresultstable">`` with columns
    Name, Title, Location, Dept, Phone, Primary Assignment, Litigation Areas.

    **Legacy (pre-2025):** ``<table id="GridView1">`` with columns
    Last Name, First Name, Title, Courthouse, Department, Phone, Primary Assignment.

    The current format is tried first.  If not found, the parser falls back to
    the legacy format for backward compatibility with archived snapshots.

    Returns an empty list if neither table format is found or has no data rows.
    """
    soup = BeautifulSoup(html, "lxml")

    # Try current format first
    table = soup.find("table", class_=TABLE_CLASS)
    if table is not None:
        officers = _parse_new_format(table)
        logger.info("Parsed judicial officers (new format)", count=len(officers))
        return officers

    # Fall back to legacy format
    table = soup.find("table", id=LEGACY_TABLE_ID)
    if table is not None:
        officers = _parse_legacy_format(table)
        logger.info("Parsed judicial officers (legacy format)", count=len(officers))
        return officers

    logger.warning(
        "Judicial officers table not found",
        table_class=TABLE_CLASS,
        legacy_table_id=LEGACY_TABLE_ID,
    )
    return []


# ---------------------------------------------------------------------------
# Weekly Judicial Directory PDF (live source since 2026-11, #4874)
# ---------------------------------------------------------------------------


@dataclass
class DirectoryOfficer:
    """One officer row from the weekly Judicial Directory PDF."""

    department: str
    full_name: str  # "First M. Last [Jr.|III]", title and role suffixes stripped
    title: str  # "Judge", "Commissioner", ...
    courthouse: str  # "Stanley Mosk", "Santa Monica", ...
    region: str  # "CENTRAL", "WEST", ...
    primary_assignment: str  # "Civil", "Family Law", ...


# Officer cell title prefixes, longest first so "Judge Pro Tem" wins over "Judge".
_OFFICER_TITLE_RE = re.compile(
    r"^(?P<title>Temporary Judge|Judge Pro Tem|Judge|Commissioner|Referee|Hearing Officer)\s+"
    r"(?P<rest>\S.*)$"
)

# A parenthetical role note, closed or left open at the end of the cell:
# "(SJ)", "(ASJ)", "(SJ- Probate/Mental Health)", "(STJ".
_PAREN_NOTE_RE = re.compile(r"\([^)]*(?:\)|$)")

# Department identifiers: "215", "014", "A", "MU", "F46", "S01", "615A", "56W", "APJ222".
_DEPT_TOKEN_RE = re.compile(r"^(?=\w)[A-Z]{0,3}\d{0,4}[A-Z]?$")

# Courthouse heading: "Stanley Mosk (VOIP: 55)".
_COURTHOUSE_RE = re.compile(r"^(?P<name>.+?)\s*\(VOIP:\s*\d+\)$")

# Region heading: "CENTRAL", "SOUTH CENTRAL", "NORTH VALLEY".
_REGION_RE = re.compile(r"^[A-Z]+(?: [A-Z]+)*$")

# Two words on the same text line can differ in ``top`` by a fraction of a point.
_LINE_TOLERANCE = 3.0
# Column boundaries come from the header row's word x-positions; allow a little slack.
_COLUMN_SLACK = 4.0


def clean_officer_name(cell_text: str) -> tuple[str, str]:
    """Split a directory officer cell into ``(title, name)``.

    Strips the title prefix and any parenthetical role note, closed or not.
    Generational suffixes stay at the end, where name normalization expects them.

    Examples:
        "Judge William H. Forman"            -> ("Judge", "William H. Forman")
        "Judge Shelley Kaufman (SJ)"         -> ("Judge", "Shelley Kaufman")
        "Judge Brenda J. Penny (SJ- Probate/Mental Health)"
                                             -> ("Judge", "Brenda J. Penny")
        "Commissioner Sarah Reisman"         -> ("Commissioner", "Sarah Reisman")
        "Judge Charles Q. Clay III"          -> ("Judge", "Charles Q. Clay III")

    Returns ``("", "")`` when the cell does not start with a known title.
    """
    text = " ".join(cell_text.split())
    match = _OFFICER_TITLE_RE.match(text)
    if match is None:
        return "", ""
    name = _PAREN_NOTE_RE.sub(" ", match.group("rest"))
    name = " ".join(name.replace(" ,", ",").split()).strip(" ,")
    return match.group("title"), name


@dataclass
class _Columns:
    """x-positions of the directory table's column starts, from its header row."""

    rm_x: float
    officer_x: float
    primary_x: float
    litigation_x: float


def _words_between(line: list[dict[str, Any]], start_x: float, end_x: float) -> list[str]:
    """Return the text of the words in ``line`` whose x0 falls in one column."""
    return [w["text"] for w in line if start_x - _COLUMN_SLACK <= w["x0"] < end_x - _COLUMN_SLACK]


def _group_lines(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group pdfplumber words into text lines (top-to-bottom, left-to-right)."""
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(word["top"] - lines[-1][0]["top"]) <= _LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _header_columns(line: list[dict[str, Any]]) -> _Columns | None:
    """Return column positions if ``line`` is a ``Dept | Rm | ... | Primary`` header row."""
    x_by_text = {w["text"]: w["x0"] for w in line}
    required = {"Rm", "Judicial", "Primary", "Litigation"}
    if line[0]["text"] != "Dept" or not required <= x_by_text.keys():
        return None
    return _Columns(
        rm_x=x_by_text["Rm"],
        officer_x=x_by_text["Judicial"],
        primary_x=x_by_text["Primary"],
        litigation_x=x_by_text["Litigation"],
    )


@dataclass
class _PendingRow:
    department: str
    officer_parts: list[str]
    courthouse: str
    region: str
    primary_parts: list[str]


def _finish_row(row: _PendingRow) -> DirectoryOfficer | None:
    title, name = clean_officer_name(" ".join(row.officer_parts))
    if not name:
        return None
    return DirectoryOfficer(
        department=row.department,
        full_name=name,
        title=title,
        courthouse=row.courthouse,
        region=row.region,
        primary_assignment=" ".join(row.primary_parts),
    )


def parse_judicial_directory_pdf(pdf_bytes: bytes) -> list[DirectoryOfficer]:
    """Parse the weekly LA Judicial Directory PDF into officer rows.

    Rows are returned in document order; a department that lists several
    officers yields one row per officer. Only text below a ``Dept | Rm |
    Phone | Judicial Officer | Primary Assignment`` header row is parsed, so
    the page banner, the alphabetical index and the location legend are
    skipped. Column boundaries come from each header row's word positions.

    Returns an empty list if the PDF cannot be read.
    """
    rows: list[_PendingRow] = []
    region = ""
    courthouse = ""
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                columns: _Columns | None = None
                pending: _PendingRow | None = None  # the row a wrapped line belongs to
                for line in _group_lines(page.extract_words()):
                    text = " ".join(w["text"] for w in line)

                    header = _header_columns(line)
                    if header is not None:
                        columns, pending = header, None
                        continue

                    heading = _COURTHOUSE_RE.match(text)
                    if heading is not None:
                        courthouse = heading.group("name")
                        columns, pending = None, None
                        continue

                    if _REGION_RE.match(text):
                        region, pending = text, None
                        continue

                    if columns is None:
                        continue

                    left = [w["text"] for w in line if w["x0"] < columns.rm_x - _COLUMN_SLACK]
                    officer = _words_between(line, columns.officer_x, columns.primary_x)
                    primary = _words_between(line, columns.primary_x, columns.litigation_x)

                    if (
                        len(left) == 1
                        and _DEPT_TOKEN_RE.match(left[0])
                        and _OFFICER_TITLE_RE.match(" ".join(officer))
                    ):
                        pending = _PendingRow(
                            department=left[0],
                            officer_parts=officer,
                            courthouse=courthouse,
                            region=region,
                            primary_parts=primary,
                        )
                        rows.append(pending)
                    elif not left and pending is not None:
                        # Wrapped cell text ("Probate/Mental Health)", "Prioleau").
                        pending.officer_parts.extend(officer)
                        pending.primary_parts.extend(primary)
                    else:
                        pending = None
    except Exception:
        logger.error("Failed to parse LA judicial directory PDF", exc_info=True)
        return []

    officers = [officer for officer in map(_finish_row, rows) if officer is not None]
    logger.info("Parsed LA judicial directory PDF", count=len(officers))
    return officers


def build_department_judge_map(
    officers: Sequence[JudicialOfficer | DirectoryOfficer],
    *,
    prefer_assignment: str | None = None,
) -> dict[str, str]:
    """Build a normalized-department → judge-full-name mapping.

    Department numbers are normalized (leading zeros stripped) so lookups
    work regardless of padding. If multiple officers share the same department,
    the first one encountered wins (the directory is authoritative).

    The mapping is keyed by department alone, but LA reuses department ids
    across courthouses ("A", "3", "400" each exist at several). With
    ``prefer_assignment`` set (e.g. ``"Civil"``), officers whose primary
    assignment matches are considered first, so a colliding id resolves to
    the officer in that kind of department. Order is otherwise preserved.

    Args:
        officers: Officer records from ``parse_judicial_directory_pdf`` or
            ``parse_judicial_officers_html``.
        prefer_assignment: Primary assignment to rank first on collisions.

    Returns:
        Dict mapping normalized department strings to full judge names.
    """
    if prefer_assignment is not None:
        officers = sorted(officers, key=lambda o: o.primary_assignment != prefer_assignment)
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


def _fetch_and_parse_directory(timeout: float = 60.0) -> tuple[bytes, dict[str, str]]:
    """Fetch and parse the weekly LA Judicial Directory PDF (shared helper).

    Makes a single HTTP GET for :data:`JUDICIAL_DIRECTORY_PDF_URL`, parses the
    officer rows, and builds the department-to-judge mapping. A PDF that
    parses to no departments returns an empty mapping; the caller decides
    what that means (``CourtDirectory.fetch_and_snapshot`` refuses to save it).

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (raw_pdf_bytes, dept_to_judge_mapping).

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
        ValueError: If the response is not a PDF (e.g. an HTML error page).
    """
    logger.info("Fetching LA judicial directory PDF", url=JUDICIAL_DIRECTORY_PDF_URL)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Judgemind/1.0 (+https://judgemind.org/scraper)"},
    ) as client:
        response = client.get(JUDICIAL_DIRECTORY_PDF_URL)
        response.raise_for_status()

    raw = response.content
    if not raw.startswith(b"%PDF"):
        msg = (
            f"LA judicial directory {JUDICIAL_DIRECTORY_PDF_URL} returned "
            f"{response.headers.get('content-type', 'unknown content-type')} "
            f"({len(raw)} bytes), not a PDF -- the report may have moved; "
            "update JUDICIAL_DIRECTORY_PDF_URL in la_dept_judges.py"
        )
        raise ValueError(msg)
    officers = parse_judicial_directory_pdf(raw)
    # The mapping feeds the civil tentative-rulings scraper, so a department id
    # shared by several courthouses resolves to its civil officer first.
    dept_map = build_department_judge_map(officers, prefer_assignment=CIVIL_ASSIGNMENT)
    logger.info(
        "Built LA department-judge mapping",
        departments=len(dept_map),
        officers=len(officers),
    )
    return raw, dept_map


class LACourtDirectory(CourtDirectory):
    """LA County Superior Court department-to-judge directory with snapshotting.

    Implements ``CourtDirectory.fetch_current()`` by downloading the weekly
    Judicial Directory PDF and parsing its officer rows into a
    department-to-judge mapping.  The base class handles S3 archival (as
    ``.pdf``), DB storage, content-hash deduplication, and refusing to save
    an empty mapping (#4880).

    Parameters
    ----------
    s3_client : object
        A boto3 S3 client for archiving raw directory responses.
    s3_bucket : str
        The S3 bucket name for archival.
    db_conn : psycopg.Connection
        A psycopg3 connection for reading/writing snapshots.
    timeout : float
        HTTP request timeout in seconds (default 60.0).
    """

    #: Court identifier used for S3 keys and DB records.
    COURT_ID: str = "ca_los_angeles"

    def __init__(
        self,
        s3_client: object,
        s3_bucket: str,
        db_conn: psycopg.Connection,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(s3_client, s3_bucket, db_conn)
        self._timeout = timeout

    def fetch_current(self) -> tuple[bytes, dict[str, str]]:
        """Fetch the live LA Judicial Directory PDF.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_pdf_bytes, dept_to_judge_mapping).
        """
        return _fetch_and_parse_directory(self._timeout)

    def fetch_and_snapshot(self, court_id: str | None = None) -> dict[str, str]:
        """Fetch the live directory and save a snapshot.

        Overrides the base class only to default ``court_id`` to
        :attr:`COURT_ID` (``"ca_los_angeles"``); the base class's
        empty-roster guard (#4880) still applies.

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
    timeout: float = 60.0,
) -> dict[str, str]:
    """Fetch the LA Judicial Directory PDF and return a dept→judge mapping.

    Makes a single HTTP GET for the weekly directory PDF, parses the
    officer rows, and builds the department-to-judge mapping.

    This is a convenience function that does **not** perform snapshotting.
    For production use with archival, use :class:`LACourtDirectory` instead.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Dict mapping normalized department strings to full judge names.

    Raises:
        httpx.HTTPStatusError: If the HTTP request fails.
        ValueError: If the response is not a PDF.
    """
    _raw, dept_map = _fetch_and_parse_directory(timeout)
    return dept_map
