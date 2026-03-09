"""Shared party name utilities.

Functions for splitting multi-party strings into individual names,
detecting name fragments, and related helpers. Used by both the
LA tentative rulings scraper and the backfill_parties script.
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


def is_name_fragment(name: str) -> bool:
    """Return True if *name* is a fragment that should not be a standalone party.

    Rejects:
    - Corporate suffixes alone (Inc, LLC, Corp, Ltd, etc.)
    - Single words shorter than 3 characters
    - Names that look like incomplete fragments (single word, no space)
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
