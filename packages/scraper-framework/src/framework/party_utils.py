"""Shared party name utilities.

Functions for splitting multi-party strings into individual names,
detecting name fragments, detecting contaminated party names (court headers,
ruling text, motion descriptions), and related helpers. Used by the
LA tentative rulings scraper, the ingestion DB layer, and backfill scripts.
"""

from __future__ import annotations

import re

# Corporate suffix patterns that should NOT trigger a comma split.
# Matches ", Inc", ", LLC", etc. at the end of a name or before another comma.
CORP_SUFFIX_RE = re.compile(
    r",\s*(?:Inc|LLC|LLP|L\.?P\.?|Corp|Corporation|Ltd|Co|Company"
    r"|N\.?A\.?|P\.?C\.?|PLLC|PLC)\.?(?=\s*(?:,|$))",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Contamination detection — patterns that indicate a "party name" is actually
# court header text, ruling body text, motion descriptions, or other non-party
# content that was incorrectly extracted as a party name.
# ---------------------------------------------------------------------------

# Court header patterns (LA-style "Department N Law And Motion Rulings ...")
_COURT_HEADER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Law And Motion Rulings?", re.IGNORECASE),
    re.compile(r"Superior Court Of", re.IGNORECASE),
    re.compile(r"Hearing Date\s*:", re.IGNORECASE),
    re.compile(r"Hearing Time\s*:", re.IGNORECASE),
    re.compile(r"Case Number\s*:", re.IGNORECASE),
    re.compile(r"Case No\.?\s*:", re.IGNORECASE),
]

# Ruling/legal text patterns
_RULING_TEXT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\[?Tentative\]?\s*Ruling", re.IGNORECASE),
    re.compile(r"Before The Court", re.IGNORECASE),
    re.compile(r"Decl\.\s*Ex\.", re.IGNORECASE),
    re.compile(r"The Same Section Further States", re.IGNORECASE),
    re.compile(r"Ruling Re\s*:", re.IGNORECASE),
    re.compile(r"The Court (?:Finds|Orders|Rules|Notes|Grants|Denies)", re.IGNORECASE),
    re.compile(r"(?:Italics|Emphasis) Added", re.IGNORECASE),
]

# Motion description patterns — only match when the entire string looks like
# a motion description rather than a party name that happens to contain
# "Motion" (e.g. "Ford Motor" is a valid party name).
_MOTION_ONLY_RE = re.compile(r"^(?:.*\s)?Motion (?:For|To|Of|Re)\b", re.IGNORECASE)

# Maximum reasonable party name length.  Real party names are rarely > 100
# characters.  Court headers and ruling text fragments are typically much
# longer.
_MAX_PARTY_NAME_CHARS = 150


def is_contaminated_party_name(name: str) -> bool:
    """Return True if *name* contains court headers, ruling text, or other non-party content.

    This function detects party names that are actually:
    - Court headers (e.g. "Department 50 Law And Motion Rulings Case Number: ...")
    - Ruling text fragments (e.g. "Before The Court Are The Following ...")
    - Motion descriptions without a real party name (e.g. "Motion For Attorney")
    - Case number + motion combos (e.g. "Inc. 30-2024-01404258 Motion to Be Relieved")
    - Very long strings that exceed reasonable party name length

    Called by ``is_name_fragment()`` and as a safety net in the DB ingestion
    layer (``batch_upsert_parties``, ``upsert_party``).
    """
    stripped = name.strip()
    if not stripped:
        return False  # Empty names handled elsewhere by is_name_fragment

    # Very long names are almost certainly contaminated text
    if len(stripped) > _MAX_PARTY_NAME_CHARS:
        return True

    # Court header patterns
    for pattern in _COURT_HEADER_PATTERNS:
        if pattern.search(stripped):
            return True

    # Ruling/legal text patterns
    for pattern in _RULING_TEXT_PATTERNS:
        if pattern.search(stripped):
            return True

    # Motion-only pattern: reject if the string is primarily a motion
    # description.  We check that it starts with or is dominated by the
    # motion phrase.  "Ford Motor Company" is fine (no "Motion For/To/Of").
    if _MOTION_ONLY_RE.search(stripped):
        return True

    return False


def is_name_fragment(name: str) -> bool:
    """Return True if *name* is a fragment that should not be a standalone party.

    Rejects:
    - Corporate suffixes alone (Inc, LLC, Corp, Ltd, etc.)
    - Single words shorter than 3 characters
    - Names that look like incomplete fragments (single word, no space)
    - Contaminated names (court headers, ruling text, motion descriptions)
    """
    stripped = name.strip().rstrip(".,;: ")
    if not stripped:
        return True

    upper = stripped.upper().rstrip(".")
    # Standalone corporate suffixes
    corp_suffixes = {
        "INC",
        "LLC",
        "LLP",
        "LP",
        "CORP",
        "CORPORATION",
        "LTD",
        "CO",
        "COMPANY",
        "NA",
        "PC",
        "PLLC",
        "PLC",
    }
    if upper in corp_suffixes:
        return True

    # Single word with no space — likely a fragment (first name only, etc.)
    # Allow single-word org names that are long enough (e.g. "Google")
    if " " not in stripped and len(stripped) < 4:
        return True

    # Contaminated party names (court headers, ruling text, etc.)
    if is_contaminated_party_name(stripped):
        return True

    return False


def split_party_names(text: str) -> list[str]:
    """Split a string containing multiple party names into individual names.

    Handles patterns like:
    - "David Keichline, Claudia Lopez, and Mason Keichline"
    - "Ashley Willowbrook LP and Ashley Willowbrook GP LP"
    - "Techno-Advanced, Inc." (corporate suffix kept with name)

    Uses ", " as the primary delimiter. Also splits on " and " when it
    appears after a comma-separated list (Oxford comma pattern).

    Corporate suffixes (Inc, LLC, Corp, etc.) preceded by commas are
    protected from splitting so "Techno-Advanced, Inc." stays intact.
    """
    # Protect corporate suffixes from comma-splitting by replacing the comma
    # with a placeholder.  E.g. "Techno-Advanced, Inc." -> "Techno-Advanced\x00 Inc."
    placeholder = "\x00"
    protected = CORP_SUFFIX_RE.sub(lambda m: m.group(0).replace(",", placeholder, 1), text)

    # First, handle Oxford comma: "A, B, and C" -> split on ", " and ", and "
    parts = re.split(r",\s+and\s+|,\s+", protected)
    # If no commas found, try splitting on standalone " and "
    if len(parts) == 1:
        parts = re.split(r"\s+and\s+", protected)

    # Restore placeholders and filter fragments
    result: list[str] = []
    for p in parts:
        restored = p.replace(placeholder, ",").strip()
        if restored and not is_name_fragment(restored):
            result.append(restored)
    return result
