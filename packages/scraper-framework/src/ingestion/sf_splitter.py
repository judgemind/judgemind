"""San Francisco document splitter for multi-case calendar pages.

Splits SF tentative ruling PDFs (which contain all cases for a department on a
given hearing date) into individual per-case rulings.  Registered with the
document splitting framework via ``register_splitter("CA", "San Francisco", ...)``.

SF Family Court PDFs have a consistent structure:

  - Pages 1-2 are boilerplate instructions (no case number) and are discarded.
  - Each case section starts with a court header block::

        SUPERIOR COURT OF CALIFORNIA
        COUNTY OF SAN FRANCISCO
        UNIFIED FAMILY COURT

  - Followed by a caption block with case number, parties, hearing date,
    department, and judge, then a motion type heading, and ``TENTATIVE RULING``
    marker before the ruling text.

The splitter splits on the court header block, discards chunks without a
case number (boilerplate), and extracts case_number, case_title, and
motion_type from each valid chunk.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from ingestion.splitter import SplitResult, register_splitter

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Court header block that marks the start of each case section.
# Line numbers from pdfplumber are embedded in the text (e.g. "1 SUPERIOR COURT...").
_HEADER_RE = re.compile(
    r"(?:^|\n)\s*\d*\s*SUPERIOR\s+COURT\s+OF\s+CALIFORNIA\s*\n"
    r"\s*\d*\s*COUNTY\s+OF\s+SAN\s+FRANCISCO\s*\n"
    r"\s*\d*\s*UNIFIED\s+FAMILY\s+COURT",
    re.MULTILINE,
)

# Case number format: F + 2 uppercase letters + hyphen + 2-digit year + hyphen + 6 digits
# Same pattern used by the SF scraper (sf_tentatives.py).
_CASE_NUMBER_RE = re.compile(r"\bF[A-Z]{2}-\d{2}-\d{6}\b")

# Motion type text appears between the "Respondent" line and "TENTATIVE RULING" marker.
_MOTION_TYPE_RE = re.compile(
    r"Respondent\s*\).*?\n(.*?)(?=TENTATIVE\s+RULING)",
    re.DOTALL,
)

# Leading line-number prefix from pdfplumber: "6 " or "10 " at start of line.
_LINE_NUM_RE = re.compile(r"^\s*\d+\s+")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_motion_type(chunk: str) -> str | None:
    """Extract the motion type text from a case chunk.

    The motion type appears between the "Respondent" caption line and the
    ``TENTATIVE RULING`` marker.  Multi-line motion types are joined with
    spaces.  Line numbers and bare numeric lines are stripped.

    Returns:
        The motion type string, or ``None`` if not found.
    """
    m = _MOTION_TYPE_RE.search(chunk)
    if not m:
        return None

    raw = m.group(1).strip()
    if not raw:
        return None

    parts: list[str] = []
    for line in raw.split("\n"):
        # Strip leading line numbers
        cleaned = _LINE_NUM_RE.sub("", line).strip()
        # Skip empty lines, bare ")" delimiters, and bare numbers
        if not cleaned or cleaned == ")" or cleaned.isdigit():
            continue
        parts.append(cleaned)

    if not parts:
        return None

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Main SF splitter
# ---------------------------------------------------------------------------


def split_sf_document(event_data: dict[str, Any]) -> list[SplitResult]:
    """Split a San Francisco multi-case calendar page into individual rulings.

    Splits on the court header block (SUPERIOR COURT OF CALIFORNIA / COUNTY OF
    SAN FRANCISCO / UNIFIED FAMILY COURT).  Chunks without a case number
    (boilerplate instruction pages) are discarded.

    Args:
        event_data: The full event dict from the ingestion pipeline.  Expected
            key: ``ruling_text``.

    Returns:
        A list of ``SplitResult`` objects.  An empty or single-element list
        signals "no splitting needed" to the framework.
    """
    # Import here to avoid circular imports at module level.
    from courts.ca.sf_tentatives import _sf_case_title_from_pdf_text

    ruling_text = event_data.get("ruling_text")
    if not ruling_text:
        return []

    # Find all header positions to split the text into chunks.
    matches = list(_HEADER_RE.finditer(ruling_text))
    if len(matches) < 2:
        return []

    results: list[SplitResult] = []
    for i, match in enumerate(matches):
        chunk_start = match.start()
        chunk_end = matches[i + 1].start() if i + 1 < len(matches) else len(ruling_text)
        chunk = ruling_text[chunk_start:chunk_end].strip()

        # Extract case number — chunks without one are boilerplate.
        case_numbers = _CASE_NUMBER_RE.findall(chunk)
        if not case_numbers:
            continue

        case_number = case_numbers[0]

        # Extract case title using the existing SF caption parser.
        case_title = _sf_case_title_from_pdf_text(chunk)

        # Extract motion type from between respondent line and TENTATIVE RULING.
        motion_type = _extract_motion_type(chunk)

        results.append(
            SplitResult(
                ruling_text=chunk,
                case_title=case_title,
                case_number=case_number,
                motion_type=motion_type,
            )
        )

    if results:
        logger.info(
            "Split SF document",
            count=len(results),
        )

    return results


# Register with the splitting framework at import time.
register_splitter("CA", "San Francisco", split_sf_document)
