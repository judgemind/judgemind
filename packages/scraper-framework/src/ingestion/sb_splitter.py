"""San Bernardino document splitter for multi-case calendar pages.

Splits SB tentative ruling PDFs (which may contain multiple cases for a
department on a given hearing date) into individual per-case rulings.
Registered with the document splitting framework via
``register_splitter("CA", "San Bernardino", ...)``.

Two multi-case formats are handled:

Format 1 (S24 style):
    Each case section starts with a ``TENTATIVE RULING FOR CIV...`` header
    followed by a department line.  Example::

        TENTATIVE RULING FOR CIVSB2416631
        Department S24 - Judge Carlos M. Cabrera

Format 2 (S22 style):
    Cases are delimited by horizontal rule lines (10+ underscores) with the
    case number and case title between two horizontal rules::

        ____________________________________________________________________________
        CIVSB2419120
        LORENZO SOLIS v. GENERAL MOTORS LLC
        ____________________________________________________________________________
        TENTATIVE RULING

When only one case is found (or no multi-case pattern is detected), the
document passes through unsplit.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from ingestion.extract import _looks_like_motion_text
from ingestion.splitter import SplitResult, register_splitter

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Case number format: CIV + 2 uppercase letters + optional space + 5-8 digits.
# Same pattern as sb_tentatives._CASE_NUMBER_RE.
_CASE_NUMBER_RE = re.compile(r"\bCIV[A-Z]{2}\s*\d{5,8}\b")

# Format 1: "TENTATIVE RULING FOR CIV..." header at the start of a case section.
_HEADER_RE = re.compile(
    r"^TENTATIVE\s+RULING\s+FOR\s+(CIV[A-Z]{2}\s*\d{5,8})\b",
    re.MULTILINE,
)

# Format 2: horizontal rule (10+ underscores) used as a delimiter.
_HR_RE = re.compile(r"^_{10,}\s*$", re.MULTILINE)

# Case title: "Name v. Name" or "Name v Name" pattern after a case number line.
_CASE_TITLE_RE = re.compile(
    r"^(.+?\s+v\.?\s+.+?)$",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_case_number(raw: str) -> str:
    """Remove internal whitespace from a case number (e.g. 'CIVSB 2600093' -> 'CIVSB2600093')."""
    return raw.replace(" ", "")


def _extract_case_title(text: str) -> str | None:
    """Extract case title ('Plaintiff v. Defendant') from a text chunk.

    Looks for lines matching the 'X v. Y' pattern.  Returns the first match,
    or None if no title is found.

    Rejects matches that look like motion descriptions rather than case
    titles (#1245) — e.g. "Order Granting Motion to Disqualify Plaintiff".
    """
    for m in _CASE_TITLE_RE.finditer(text[:500]):
        line = m.group(1).strip()
        # Skip lines that are clearly not case titles.
        if line.startswith("TENTATIVE") or line.startswith("Department"):
            continue
        if line.startswith("Motion") or line.startswith("Movant"):
            continue
        # Must contain "v." or " v " to be a case title.
        if not re.search(r"\bv\.?\s", line, re.IGNORECASE):
            continue
        # Reject motion descriptions masquerading as case titles (#1245).
        if _looks_like_motion_text(line):
            continue
        return line
    return None


def _split_format1(text: str) -> list[SplitResult]:
    """Split using Format 1: 'TENTATIVE RULING FOR CIV...' headers.

    Returns an empty list if fewer than 2 case sections are found.
    """
    matches = list(_HEADER_RE.finditer(text))
    if len(matches) < 2:
        return []

    results: list[SplitResult] = []
    for i, match in enumerate(matches):
        chunk_start = match.start()
        chunk_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[chunk_start:chunk_end].strip()

        case_number = _normalise_case_number(match.group(1))
        case_title = _extract_case_title(chunk)

        results.append(
            SplitResult(
                ruling_text=chunk,
                case_number=case_number,
                case_title=case_title,
            )
        )

    return results


def _split_format2(text: str) -> list[SplitResult]:
    """Split using Format 2: horizontal rule delimiters with case numbers.

    Looks for patterns of HR + case number + case title + HR + TENTATIVE RULING.
    The boundary between cases is the HR line before each case number.

    Returns an empty list if fewer than 2 case sections are found.
    """
    hr_positions = list(_HR_RE.finditer(text))
    if len(hr_positions) < 2:
        return []

    # Find case boundaries: an HR followed by a case number on the next
    # non-blank line.  Each boundary marks the start of a case section.
    boundaries: list[tuple[int, str]] = []  # (hr_start_pos, case_number)
    for hr_match in hr_positions:
        # Look at the text after this HR for a case number.
        after_hr = text[hr_match.end() :].lstrip("\n")
        case_match = _CASE_NUMBER_RE.match(after_hr)
        if case_match:
            boundaries.append((hr_match.start(), _normalise_case_number(case_match.group())))

    if len(boundaries) < 2:
        return []

    results: list[SplitResult] = []
    for i, (start_pos, case_number) in enumerate(boundaries):
        end_pos = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        chunk = text[start_pos:end_pos].strip()

        case_title = _extract_case_title(chunk)

        results.append(
            SplitResult(
                ruling_text=chunk,
                case_number=case_number,
                case_title=case_title,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Main SB splitter
# ---------------------------------------------------------------------------


def split_sb_document(event_data: dict[str, Any]) -> list[SplitResult]:
    """Split a San Bernardino multi-case calendar page into individual rulings.

    Tries Format 1 (TENTATIVE RULING FOR CIV... headers) first, then falls
    back to Format 2 (horizontal rule delimiters).  If neither format detects
    multiple cases, returns an empty list (the framework treats this as
    "no splitting needed").

    Args:
        event_data: The full event dict from the ingestion pipeline.  Expected
            key: ``ruling_text``.

    Returns:
        A list of ``SplitResult`` objects.  An empty or single-element list
        signals "no splitting needed" to the framework.
    """
    ruling_text = event_data.get("ruling_text")
    if not ruling_text:
        return []

    # Try Format 1 first (S24 style: TENTATIVE RULING FOR CIV... headers).
    results = _split_format1(ruling_text)
    if len(results) >= 2:
        logger.info(
            "Split SB document (Format 1: header)",
            count=len(results),
        )
        return results

    # Try Format 2 (S22 style: horizontal rule delimiters).
    results = _split_format2(ruling_text)
    if len(results) >= 2:
        logger.info(
            "Split SB document (Format 2: horizontal rule)",
            count=len(results),
        )
        return results

    # No multi-case pattern detected — pass through unsplit.
    return []


# Register with the splitting framework at import time.
register_splitter("CA", "San Bernardino", split_sb_document)
