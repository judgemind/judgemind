"""Orange County document splitter for multi-case calendar pages.

Splits OC tentative ruling PDFs (which contain all cases for a department on a
given date) into individual per-case rulings.  Registered with the document
splitting framework via ``register_splitter("CA", "Orange", ...)``.

Two sub-formats are handled:

North Justice Center (departments starting with "N"):
    Numbered 1-3 digit line entries (1, 2, ... or 101, 102, ...) with case
    names containing "vs".  No case numbers in the PDF text.  Entry boundaries
    are identified by the numbered-entry pattern, pre-filtered to only include
    lines where "vs" appears nearby (to avoid false positives from prose lines
    that happen to start with a number).

Central / West / Costa Mesa / Complex (all other OC departments):
    Numbered entries (1-digit, 2-digit, or 3-digit) where a case number
    (matching ``\\b\\d{2,4}-\\d{7,8}\\b``) appears near the entry start.
    Boundaries are identified by the numbered-entry + case-number pattern.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from ingestion.splitter import SplitResult, register_splitter

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# North Justice Center splitting
# ---------------------------------------------------------------------------

# Entry-start regex for North JC — 1-3 digit line numbers.
# Most North departments use 3-digit numbers (101, 102, ...) but some
# (e.g. N17, Judge Griffin) use single-digit (1, 2, 3, ...).
_NORTH_ENTRY_START_RE = re.compile(r"^(\d{1,3})\s+(.+)", re.MULTILINE)

# Motion-type keywords (same as oc_tentatives._MOTION_KEYWORDS).
_MOTION_KEYWORDS = (
    "Motion for",
    "Motion to",
    "Demurrer",
    "OSC",
    "Application",
    "Petition",
    "Status Conference",
    "Case Management Conference",
    "CMC REMAINS",
    "OFF CALENDAR",
    "HEARING",
)


def _is_north_case_entry(lines: list[str], line_idx: int) -> bool:
    """Return True if this numbered line looks like a North JC case entry.

    Checks whether "vs" appears on this line or within the next few
    continuation lines, which indicates a case name (plaintiff vs defendant).
    This filters out false positives like "10 calendar days" or legal
    citations like "151 Cal.App.4th 168".
    """
    # Check the entry line itself and up to 5 continuation lines.
    search_end = min(line_idx + 6, len(lines))
    nearby_text = " ".join(lines[line_idx:search_end])
    return bool(re.search(r"\bvs\.?\b", nearby_text, re.IGNORECASE))


def _split_north(text: str) -> list[SplitResult]:
    """Split a North Justice Center calendar page into per-case rulings.

    Identifies case entry boundaries using 1-3 digit line numbers, then
    extracts the ruling text for each case.  Only entries whose nearby text
    contains "vs" are treated as case entries (filtering out legal citations
    and prose lines that happen to start with a number).

    Returns an empty list if fewer than 2 case entries are found (the caller
    treats this as "no splitting needed").
    """
    lines = text.split("\n")

    # Find all entry start positions that look like actual case entries.
    # Pre-filtering by "vs" is critical: with \d{1,3}, many prose lines
    # match the regex (e.g. "10 calendar days", "2 and 3, Request for").
    # Only lines where "vs" appears nearby are genuine case entries.
    entry_positions: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        m = _NORTH_ENTRY_START_RE.match(line)
        if m and _is_north_case_entry(lines, i):
            entry_positions.append((i, m.group(1), m.group(2)))

    results: list[SplitResult] = []
    for idx, (line_idx, line_num, first_line_rest) in enumerate(entry_positions):
        # Where does the next entry start?
        if idx + 1 < len(entry_positions):
            next_entry_line = entry_positions[idx + 1][0]
        else:
            next_entry_line = len(lines)

        # Parse case name and motion type from the first line.
        motion_start = len(first_line_rest)
        motion_type: str | None = None
        for kw in _MOTION_KEYWORDS:
            pos = first_line_rest.find(kw)
            if pos != -1 and pos < motion_start:
                motion_start = pos
                motion_type = first_line_rest[pos:].strip()

        case_name_part = first_line_rest[:motion_start].strip()

        # Collect continuation lines for multi-line case names.
        name_parts = [case_name_part]
        for j in range(line_idx + 1, min(next_entry_line, line_idx + 6)):
            cand = lines[j].strip()
            if not cand or cand.startswith("Page "):
                break
            if len(cand) >= 35:
                break
            if "." in cand or "(" in cand or ")" in cand:
                break
            name_parts.append(cand)

        full_name = " ".join(name_parts)
        full_name = re.sub(r"\s+", " ", full_name).strip()

        # Only keep entries that contain "vs" — actual cases.
        if not re.search(r"\bvs\.?\b", full_name, re.IGNORECASE):
            continue

        # Extract ruling text: everything from this entry to the next.
        ruling_lines = lines[line_idx:next_entry_line]
        ruling_text = "\n".join(ruling_lines).strip()

        results.append(
            SplitResult(
                ruling_text=ruling_text,
                case_title=full_name,
                case_number=None,  # North JC PDFs have no case numbers
                motion_type=motion_type,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Central / West / Costa Mesa / Complex splitting (case-number based)
# ---------------------------------------------------------------------------

# Matches a numbered entry line.  The number can be 1-3 digits, optionally
# followed by a period and/or ampersand.  Examples:
#   "1 30-2024-01393434 1. Case Management Conference"
#   "3. 30-2024-01447336 1. Motion to Be Relieved..."
#   "100 Hedayati vs. US Kitchen ..."
#   "1. Creditors Before the Court is a ..."
_NUMBERED_ENTRY_RE = re.compile(
    r"^(?P<num>\d{1,3})\.?\s+(?P<rest>.+)",
    re.MULTILINE,
)

# Case number pattern (same as the OC scraper).
_CASE_NUMBER_RE = re.compile(r"\b\d{2,4}-\d{7,8}\b")

# Three-part case number: location-year-sequence, e.g. "30-2024-01393434".
_THREE_PART_RE = re.compile(r"\b(\d{2})-(\d{4}-\d{7,8})\b")

# Header line patterns to skip when looking for case boundaries.
_HEADER_RE = re.compile(
    r"^(?:TENTATIVE RULINGS|LAW\s*[&]\s*MOTION|DEPT |JUDGE |Date:|#\s+Case\s+Name|"
    r"#\s+CASE\s+NAME|Page \d+ of \d+|$)",
    re.IGNORECASE,
)


def _find_case_number_near(lines: list[str], start: int, end: int) -> str | None:
    """Find the first case number in the given line range.

    Checks for three-part numbers first (30-2024-01393434), then falls back
    to the standard two-part format.
    """
    for i in range(start, min(end, len(lines))):
        # Prefer three-part match
        m3 = _THREE_PART_RE.search(lines[i])
        if m3:
            return f"{m3.group(1)}-{m3.group(2)}"
        m2 = _CASE_NUMBER_RE.search(lines[i])
        if m2:
            return m2.group(0)
    return None


def _extract_case_title_from_entry(first_line_rest: str) -> str | None:
    """Extract a case title (plaintiff vs defendant) from a numbered entry line.

    Returns the case title if the line contains "vs", otherwise None.
    """
    # Remove a leading case number if present.
    cleaned = _CASE_NUMBER_RE.sub("", first_line_rest).strip()
    cleaned = _THREE_PART_RE.sub("", cleaned).strip()

    # Remove leading "& " or number prefixes (e.g. "& 2 Kaufman vs. ...")
    cleaned = re.sub(r"^[&\d.\s]+", "", cleaned).strip()

    # Look for "vs" to find the case title part.
    if not re.search(r"\bvs\.?\b", cleaned, re.IGNORECASE):
        return None

    # The case title is typically the text before the motion type.
    for kw in _MOTION_KEYWORDS:
        pos = cleaned.find(kw)
        if pos > 0:
            return cleaned[:pos].strip()

    return cleaned.strip() if cleaned else None


def _is_sub_motion_line(rest: str) -> bool:
    """Return True if the line text looks like a sub-motion item, not a case entry.

    Sub-motions are numbered items within a case (e.g. "2. Motion to Compel ..."
    or "2. Status Conference") that start directly with a motion keyword or
    common calendar item type.  These should not be treated as new case entries.
    """
    cleaned = rest.strip()
    # Remove leading sub-item numbers like "1. " from multi-motion entries.
    cleaned = re.sub(r"^\d+\.\s*", "", cleaned)

    sub_motion_prefixes = (
        "Motion ",
        "Demurrer",
        "OSC",
        "Application",
        "Petition",
        "Status Conference",
        "Case Management Conference",
        "CMC",
        "OFF CALENDAR",
        "HEARING",
        "Order to Show",
    )
    for prefix in sub_motion_prefixes:
        if cleaned.startswith(prefix):
            return True
    return False


def _split_case_number_based(text: str) -> list[SplitResult]:
    """Split a non-North OC calendar page using case number boundaries.

    Identifies case entries by finding numbered lines (1., 2., 3. etc.) that
    have an associated case number within the first few lines of the entry.
    Extracts the ruling text between consecutive case boundaries.

    Returns an empty list if fewer than 2 case entries are found.
    """
    lines = text.split("\n")

    # First pass: find all numbered entry positions with associated case numbers.
    entries: list[tuple[int, int, str, str | None, str | None]] = []
    # (line_index, entry_number, first_line_rest, case_number, case_title)

    for i, line in enumerate(lines):
        m = _NUMBERED_ENTRY_RE.match(line)
        if not m:
            continue

        num = int(m.group("num"))
        rest = m.group("rest")

        # Skip header lines and legal citations.
        if _HEADER_RE.match(line):
            continue

        # Skip lines where the "number" is actually part of prose text.
        # E.g. "2 and 3, Request for Admissions..." where "2" is a
        # reference number, not a case entry number.
        if re.match(r"^(?:and|or|to|of|the|in|at|is|was|for|on|but)\b", rest, re.IGNORECASE):
            continue

        # Check if the line has a case number directly on it.
        has_case_number_on_line = bool(_CASE_NUMBER_RE.search(line) or _THREE_PART_RE.search(line))

        # If no case number on the line itself and it looks like a sub-motion
        # (e.g. "2. Motion to Compel ..." or "2. Status Conference"), skip it.
        # These are numbered items within a case entry, not new case entries.
        if not has_case_number_on_line and _is_sub_motion_line(rest):
            continue

        # Look for a case number on this line or the next several lines.
        # Extended range (8 lines) for formats like Costa Mesa where the case
        # number may appear several lines after the entry start.
        case_number = _find_case_number_near(lines, i, min(i + 8, len(lines)))

        if case_number is None:
            # No case number nearby — this might be a legal citation or
            # boilerplate, not a case entry.  Skip it unless we can
            # identify it as a case entry by other means.
            # For formats where the entry starts with a case name (e.g. West),
            # check if it has "vs" in the nearby text.
            nearby_text = " ".join(lines[i : min(i + 4, len(lines))])
            if not re.search(r"\bvs\.?\b", nearby_text, re.IGNORECASE):
                continue

        # Extract case title if present.
        case_title = _extract_case_title_from_entry(rest)
        # Also check continuation lines for case title.
        if case_title is None:
            for j in range(i + 1, min(i + 4, len(lines))):
                ct = _extract_case_title_from_entry(lines[j])
                if ct:
                    case_title = ct
                    break

        # Validate: must have a case number OR be a recognized case entry.
        if case_number is None and case_title is None:
            continue

        entries.append((i, num, rest, case_number, case_title))

    if len(entries) < 2:
        return []

    # Second pass: extract ruling text for each entry.
    results: list[SplitResult] = []
    for idx, (line_idx, _num, rest, case_number, case_title) in enumerate(entries):
        if idx + 1 < len(entries):
            next_line_idx = entries[idx + 1][0]
        else:
            next_line_idx = len(lines)

        ruling_text = "\n".join(lines[line_idx:next_line_idx]).strip()

        # Extract motion type from the first line.
        motion_type: str | None = None
        for kw in _MOTION_KEYWORDS:
            pos = rest.find(kw)
            if pos != -1:
                motion_type = rest[pos:].strip()
                break

        results.append(
            SplitResult(
                ruling_text=ruling_text,
                case_title=case_title,
                case_number=case_number,
                motion_type=motion_type,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Main OC splitter dispatch
# ---------------------------------------------------------------------------


def _is_north_dept(dept: str | None) -> bool:
    """Return True if the department code indicates North Justice Center."""
    if not dept:
        return False
    return dept.upper().strip().startswith("N")


def split_oc_document(event_data: dict[str, Any]) -> list[SplitResult]:
    """Split an Orange County multi-case calendar page into individual rulings.

    Dispatches to the North JC splitter or the case-number-based splitter
    depending on the department code.

    Args:
        event_data: The full event dict from the ingestion pipeline.  Expected
            keys: ``ruling_text``, ``department``.

    Returns:
        A list of ``SplitResult`` objects.  An empty or single-element list
        signals "no splitting needed" to the framework.
    """
    ruling_text = event_data.get("ruling_text")
    if not ruling_text:
        return []

    department = event_data.get("department")

    if _is_north_dept(department):
        results = _split_north(ruling_text)
        if results:
            logger.info(
                "Split North JC document",
                department=department,
                count=len(results),
            )
        return results

    results = _split_case_number_based(ruling_text)
    if results:
        logger.info(
            "Split OC document by case number",
            department=department,
            count=len(results),
        )
    return results


# Register with the splitting framework at import time.
register_splitter("CA", "Orange", split_oc_document)
