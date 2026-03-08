"""Ruling text cleanup utilities for the ingestion pipeline.

Cleans raw ruling text extracted from PDF/HTML court documents before storing
in Postgres. The raw content in S3 is never modified — only the processed
ruling_text column is cleaned.

Common issues addressed:
  - Character encoding errors (mojibake from Latin-1/Windows-1252 misinterpreted as UTF-8)
  - Page number artifacts ("Page 2 of 5", "- 3 -", standalone digits)
  - Excessive blank lines
  - Leading/trailing whitespace on lines
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Encoding fix: common mojibake replacements
# ---------------------------------------------------------------------------
# When Windows-1252 text is decoded as UTF-8 (or vice versa), specific byte
# sequences produce recognizable garbage characters. This map covers the most
# common ones seen in California court PDFs.

_MOJIBAKE_MAP: dict[str, str] = {
    # Smart quotes
    "\u00e2\u0080\u009c": "\u201c",  # left double quote
    "\u00e2\u0080\u009d": "\u201d",  # right double quote
    "\u00e2\u0080\u0098": "\u2018",  # left single quote
    "\u00e2\u0080\u0099": "\u2019",  # right single quote
    # Dashes
    "\u00e2\u0080\u0093": "\u2013",  # en dash
    "\u00e2\u0080\u0094": "\u2014",  # em dash
    # Ellipsis
    "\u00e2\u0080\u00a6": "\u2026",  # horizontal ellipsis
    # Section / paragraph signs
    "\u00c2\u00a7": "\u00a7",  # section sign (double-encoded)
    "\u00c2\u00b6": "\u00b6",  # pilcrow / paragraph sign
    # Common single-char replacements
    "\u00bf": "'",  # inverted question mark -> apostrophe (common PDF artifact)
    "\u00c2\u00a0": " ",  # double-encoded non-breaking space
    "\u00a0": " ",  # non-breaking space -> regular space
    # Bullet
    "\u00e2\u0080\u00a2": "\u2022",  # bullet
}

# Pre-compile a single regex that matches any mojibake key.
# Sort by length descending so longer sequences match first.
_MOJIBAKE_PATTERN: re.Pattern[str] = re.compile(
    "|".join(re.escape(k) for k in sorted(_MOJIBAKE_MAP, key=len, reverse=True))
)

# ---------------------------------------------------------------------------
# Page number patterns
# ---------------------------------------------------------------------------

_PAGE_NUMBER_PATTERNS: list[re.Pattern[str]] = [
    # "Page 2 of 5", "PAGE 2 OF 5", "page 2 of 5"
    re.compile(r"^\s*page\s+\d+\s+of\s+\d+\s*$", re.IGNORECASE),
    # "- 3 -" or "-- 3 --" style page numbers
    re.compile(r"^\s*-{1,2}\s*\d+\s*-{1,2}\s*$"),
    # Standalone small numbers on a line (1-999), common PDF page numbers
    re.compile(r"^\s*\d{1,3}\s*$"),
]

# ---------------------------------------------------------------------------
# Boilerplate patterns (lines to remove)
# ---------------------------------------------------------------------------
# These patterns match common procedural boilerplate that appears in ruling
# text but is not substantive content.

_BOILERPLATE_PATTERNS: list[re.Pattern[str]] = [
    # "SUPERIOR COURT OF CALIFORNIA" header
    re.compile(
        r"^\s*SUPERIOR\s+COURT\s+OF\s+(?:THE\s+STATE\s+OF\s+)?CALIFORNIA\s*$",
        re.IGNORECASE,
    ),
    # "COUNTY OF LOS ANGELES" etc.
    re.compile(r"^\s*COUNTY\s+OF\s+\w[\w\s]*$", re.IGNORECASE),
    # "DEPARTMENT 1" / "DEPT 1" / "DEPT. 1" headers
    re.compile(r"^\s*(?:DEPARTMENT|DEPT\.?)\s+\S+\s*$", re.IGNORECASE),
    # Submission instruction lines
    re.compile(
        r"^\s*(?:parties\s+who\s+intend\s+to\s+submit|"
        r"if\s+you\s+intend\s+to\s+submit|"
        r"unless\s+.*\s+notif(?:y|ies)|"
        r"parties\s+should\s+notify|"
        r"the\s+court\s+will\s+prepare|"
        r"if\s+the\s+parties\s+neither)",
        re.IGNORECASE,
    ),
]


def fix_encoding(text: str) -> str:
    """Fix common mojibake / encoding errors in ruling text.

    Replaces known multi-byte garbage sequences with their correct Unicode
    equivalents. Also normalizes non-breaking spaces and other whitespace
    characters.
    """
    return _MOJIBAKE_PATTERN.sub(lambda m: _MOJIBAKE_MAP[m.group()], text)


def strip_page_numbers(text: str) -> str:
    """Remove lines that are page number artifacts.

    Operates line-by-line so that surrounding content is preserved.
    """
    lines = text.split("\n")
    cleaned: list[str] = []
    for line in lines:
        if any(p.match(line) for p in _PAGE_NUMBER_PATTERNS):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def strip_boilerplate(text: str) -> str:
    """Remove common boilerplate header/instruction lines.

    Only removes lines that match boilerplate patterns entirely — does not
    modify lines that contain substantive content mixed with boilerplate.
    """
    lines = text.split("\n")
    cleaned: list[str] = []
    for line in lines:
        if any(p.match(line) for p in _BOILERPLATE_PATTERNS):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def collapse_whitespace(text: str) -> str:
    """Collapse excessive blank lines and trailing whitespace.

    - Strips trailing whitespace from each line.
    - Collapses 3+ consecutive blank lines down to 2 (preserving paragraph breaks).
    - Strips leading/trailing blank lines from the entire text.
    """
    lines = text.split("\n")
    # Strip trailing whitespace per line
    lines = [line.rstrip() for line in lines]
    # Collapse runs of 3+ blank lines to 2
    result: list[str] = []
    blank_count = 0
    for line in lines:
        if line == "":
            blank_count += 1
            if blank_count <= 2:
                result.append(line)
        else:
            blank_count = 0
            result.append(line)
    return "\n".join(result).strip()


def clean_ruling_text(text: str | None) -> str | None:
    """Apply all cleanup transformations to ruling text.

    Returns None if the input is None or the result is empty after cleanup.
    The transformations are applied in order:
      1. Fix encoding errors (mojibake)
      2. Strip page number artifacts
      3. Strip boilerplate headers
      4. Collapse excessive whitespace

    The raw content in S3 is never modified — this only affects the
    ruling_text stored in Postgres for display.
    """
    if text is None:
        return None

    result = text
    result = fix_encoding(result)
    result = strip_page_numbers(result)
    result = strip_boilerplate(result)
    result = collapse_whitespace(result)

    return result if result else None
