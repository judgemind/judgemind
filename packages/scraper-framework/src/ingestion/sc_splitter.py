"""Santa Clara document splitter for multi-case ruling PDFs.

Splits Santa Clara tentative ruling PDFs (which contain multiple cases for a
department on a given hearing date) into individual per-case rulings.
Registered with the document splitting framework via
``register_splitter("CA", "Santa Clara", ...)``.

Santa Clara PDFs have two main formats:

Format 1 (Dept 6 style -- table layout):
    A header line ``LINE CASE NO. CASE TITLE TENTATIVE RULING`` followed by
    entries with a time prefix::

        9:00 24CV443183 Huynh vs Redis Labs Case is off calendar.
        1,
        9:00 25CV460465 Nicholas v. Compass ...
        2 Group, et. Al.

Format 2 (Dept 1 style -- LINE prefix):
    Each case starts with ``LINE N`` followed by the case number::

        LINE 1 26CV484360 Mohammad Order to Show Cause ...
                          Khokhar vs Gene Injunction
                          Kristul et al

Format 3 (Dept 16 style -- column layout):
    Each case starts with a time and line number, with the title spanning
    multiple lines::

        9:01 23CV419582 Virginia Riegos-Rangel, et al. Hearing on ...
        1              v.
                       Terry Ray Freeman, et al.

All formats share a common anchor: a case number in the ``\\d{2}(?:CV|PR)\\d{6}``
format appears near the start of each case section. The splitter detects case
boundaries by finding these case numbers preceded by time markers or LINE
prefixes.
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

# Case number: 2-digit year + CV or PR + 6 digits.
_CASE_NUMBER_RE = re.compile(r"\b(\d{2}(?:CV|PR)\d{6})\b", re.IGNORECASE)

# Case entry boundary: a line starting with optional time (9:00/9:01), optional
# line numbers, then a case number. This matches the start of each case section
# across all three department formats.
#
# Matches patterns like:
#   "9:00 24CV443183 Huynh vs Redis Labs ..."
#   "LINE 1 26CV484360 ..."
#   "9:01 23CV419582 Virginia ..."
#   "LINE 3- 25CV480596 ..."
_CASE_ENTRY_RE = re.compile(
    r"^(?:"
    r"(?:LINE\s+\d+(?:\s*[-,]\s*\d+)?\s+)"  # "LINE N" or "LINE N-M"
    r"|"
    r"(?:\d{1,2}:\d{2}\s+)"  # "9:00 " time prefix
    r")?"
    r"(\d{2}(?:CV|PR)\d{6})"  # capture case number
    r"\s+(.+)",  # capture rest of line (title + ruling start)
    re.IGNORECASE | re.MULTILINE,
)

# Outcome keywords for extraction from ruling text.
_OUTCOME_RE = re.compile(
    r"\b(?P<outcome>"
    r"GRANTED|DENIED|SUSTAINED|OVERRULED|MOOT"
    r"|OFF\s+(?:CALENDAR|calendar)"
    r"|off\s+calendar"
    r")\b",
    re.IGNORECASE,
)

# Motion type keywords.
_MOTION_TYPE_RE = re.compile(
    r"\b(?P<motion_type>"
    r"Demurrer|Motion\s+to\s+(?:Compel|Dismiss|Strike|Quash|Stay|Vacate|Set\s+Aside)"
    r"|Summary\s+Judgment|Summary\s+Adjudication"
    r"|(?:Petition|Motion)\s+(?:to\s+Compel\s+)?(?:Arbitration|Writ\s+of\s+Attachment)"
    r"|Writ\s+of\s+Attachment"
    r"|(?:Temporary\s+)?Restraining\s+Order"
    r"|Preliminary\s+Injunction"
    r"|Compromise\s+of\s+Minor(?:'s)?\s+Claim"
    r"|(?:Hearing(?:\s+on)?:?\s+)?Compromise\s+of\s+Minor(?:'s)?\s+Claim"
    r")\b",
    re.IGNORECASE,
)

# "Name v. Name" separator.
_VS_RE = re.compile(r"\s+v[s]?\.?\s+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_case_title_from_chunk(chunk: str) -> str | None:
    """Extract case title from a case chunk.

    Searches for "Party v. Party" patterns in the first few lines of the chunk.
    Returns the cleaned title or None.
    """
    # Look in the first 500 characters for a "v." or "vs" pattern
    search_area = chunk[:500]
    lines = search_area.split("\n")

    # Collect text from the first few content lines to handle titles split across lines
    combined = " ".join(line.strip() for line in lines[:6] if line.strip())

    # Look for "Name v. Name" pattern in combined text
    vs_match = _VS_RE.search(combined)
    if not vs_match:
        return None

    # Extract the title around the "v." separator
    vs_start = vs_match.start()
    vs_end = vs_match.end()

    # Find plaintiff: go backwards from "v." to the case number or start
    before_vs = combined[:vs_start]

    # Strip case number, time, and line markers from the beginning
    case_num_match = _CASE_NUMBER_RE.search(before_vs)
    if case_num_match:
        plaintiff_text = before_vs[case_num_match.end() :].strip()
    else:
        plaintiff_text = before_vs.strip()

    # Find defendant: from after "v." to the next major break
    after_vs = combined[vs_end:]
    defendant_text = after_vs

    # Cut at motion keyword / ruling text / outcome
    for pattern in [_MOTION_TYPE_RE, _OUTCOME_RE]:
        m = pattern.search(defendant_text)
        if m:
            defendant_text = defendant_text[: m.start()]
            break

    # Cut at ruling body text markers (these signal the defendant name has ended
    # and the ruling text has begun)
    ruling_start = re.search(
        r"\b(?:Petitioner|Plaintiff|Defendant|Respondent|Movant|"
        r"The\s+(?:Court|Motion|Petition|Application|Hearing)|"
        r"Before\s+the\s+Court|Case\s+is|Cases\s+have|"
        r"Hearing(?:\s+on)?:|Order\s+(?:on|Re:))\b",
        defendant_text,
    )
    if ruling_start:
        defendant_text = defendant_text[: ruling_start.start()]

    # Cut at first sentence-like break (period followed by uppercase)
    sentence_break = re.search(r"\.\s+[A-Z]", defendant_text)
    if sentence_break:
        defendant_text = defendant_text[: sentence_break.start()]

    # Cut at double space (often separates title from ruling text)
    double_space = re.search(r"\s{2,}", defendant_text)
    if double_space:
        defendant_text = defendant_text[: double_space.start()]

    # Clean up
    plaintiff_text = " ".join(plaintiff_text.split()).strip().rstrip(",;:")
    defendant_text = " ".join(defendant_text.split()).strip().rstrip(",;:")

    # Remove trailing "et" that got cut off
    defendant_text = re.sub(r"\s+et$", "", defendant_text)

    if not plaintiff_text or not defendant_text:
        return None

    # Reconstruct title
    vs_sep = combined[vs_start:vs_end].strip()
    title = f"{plaintiff_text} {vs_sep} {defendant_text}"

    # Sanity check: title should be reasonable length
    if len(title) > 200:
        title = title[:200]
    if len(title) < 5:
        return None

    return title


def _extract_outcome(text: str) -> str | None:
    """Extract outcome from ruling text."""
    m = _OUTCOME_RE.search(text)
    if m:
        raw = m.group("outcome").strip()
        if raw.lower().startswith("off"):
            return "Off calendar"
        return raw.upper()
    return None


def _extract_motion_type(text: str) -> str | None:
    """Extract motion type from ruling text."""
    m = _MOTION_TYPE_RE.search(text)
    if m:
        raw = m.group("motion_type").strip()
        normalized = " ".join(raw.split())
        if normalized and normalized[0].islower():
            normalized = normalized[0].upper() + normalized[1:]
        return normalized
    return None


def _find_case_boundaries(text: str) -> list[tuple[int, str]]:
    """Find the start position and case number of each case entry in the text.

    Returns a list of (position, case_number) tuples sorted by position.
    """
    boundaries: list[tuple[int, str]] = []
    seen_positions: set[int] = set()

    for m in _CASE_ENTRY_RE.finditer(text):
        pos = m.start()
        case_number = m.group(1).upper()
        if pos not in seen_positions:
            seen_positions.add(pos)
            boundaries.append((pos, case_number))

    return boundaries


# ---------------------------------------------------------------------------
# Main splitter
# ---------------------------------------------------------------------------


def split_sc_document(event_data: dict[str, Any]) -> list[SplitResult]:
    """Split a Santa Clara multi-case calendar PDF into individual rulings.

    Finds case boundaries by looking for case number patterns preceded by
    time stamps or LINE markers. Each case section runs from its boundary to
    the start of the next case.

    Args:
        event_data: The full event dict from the ingestion pipeline. Expected
            key: ``ruling_text``.

    Returns:
        A list of ``SplitResult`` objects. An empty or single-element list
        signals "no splitting needed" to the framework.
    """
    ruling_text = event_data.get("ruling_text")
    if not ruling_text:
        return []

    boundaries = _find_case_boundaries(ruling_text)

    if len(boundaries) < 2:
        # Single case or no boundaries found -- no splitting needed.
        return []

    results: list[SplitResult] = []
    for i, (pos, case_number) in enumerate(boundaries):
        # Chunk runs from this boundary to the next (or end of text)
        end_pos = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(ruling_text)
        chunk = ruling_text[pos:end_pos].strip()

        case_title = _extract_case_title_from_chunk(chunk)
        outcome = _extract_outcome(chunk)
        motion_type = _extract_motion_type(chunk)

        results.append(
            SplitResult(
                ruling_text=chunk,
                case_number=case_number,
                case_title=case_title,
                outcome=outcome,
                motion_type=motion_type,
            )
        )

    logger.info(
        "Split SC document",
        count=len(results),
    )
    return results


# Register with the splitting framework at import time.
register_splitter("CA", "Santa Clara", split_sc_document)
