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

# ---------------------------------------------------------------------------
# Multi-line boilerplate block patterns
# ---------------------------------------------------------------------------
# These match the *start* of a multi-line instruction block.  Everything from
# the start marker through consecutive non-blank lines is removed.

_BLOCK_START_PATTERNS: list[re.Pattern[str]] = [
    # LA County Dept 51 style: "DEPARTMENT 51 LAW AND MOTION RULINGS"
    re.compile(
        r"^\s*(?:DEPARTMENT|DEPT\.?)\s+\S+\s+LAW\s+AND\s+MOTION\s+RULINGS?\s*$",
        re.IGNORECASE,
    ),
    # "If you wish to submit on the tentative ruling..."
    re.compile(
        r"^\s*if\s+you\s+wish\s+to\s+submit\s+on\s+the\s+tentative",
        re.IGNORECASE,
    ),
    # "If you intend to submit on this tentative ruling..."
    re.compile(
        r"^\s*if\s+you\s+intend\s+to\s+submit\s+on\s+this\s+tentative",
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
    """Remove common boilerplate header/instruction lines and blocks.

    Handles two kinds of boilerplate:
      1. Single lines matching ``_BOILERPLATE_PATTERNS`` (removed individually).
      2. Multi-line instruction blocks that start with a ``_BLOCK_START_PATTERNS``
         marker.  When a start marker is found, all consecutive non-blank lines
         from that point are removed (the block ends at the first blank line or
         end of text).
    """
    lines = text.split("\n")
    cleaned: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Check for single-line boilerplate
        if any(p.match(line) for p in _BOILERPLATE_PATTERNS):
            i += 1
            continue

        # Check for multi-line block start
        if any(p.match(line) for p in _BLOCK_START_PATTERNS):
            # Skip all consecutive non-blank lines (the block)
            while i < len(lines) and lines[i].strip():
                i += 1
            continue

        cleaned.append(line)
        i += 1
    return "\n".join(cleaned)


# ---------------------------------------------------------------------------
# Paragraph detection
# ---------------------------------------------------------------------------
# PDF-extracted text often has single newlines at line breaks but no
# double-newline separators between paragraphs.  These heuristics detect
# paragraph boundaries and insert double-newline separators.

# Section headers commonly found in California court rulings.
# Must be ALL CAPS, optionally with spaces between words.
_SECTION_HEADER_PATTERN: re.Pattern[str] = re.compile(r"^\s*[A-Z][A-Z /&]+[A-Z]\s*$")

# Known section header keywords for stricter matching on short headers.
_KNOWN_HEADERS: frozenset[str] = frozenset(
    {
        "BACKGROUND",
        "DISCUSSION",
        "RULING",
        "ORDER",
        "ANALYSIS",
        "CONCLUSION",
        "LEGAL STANDARD",
        "FACTUAL BACKGROUND",
        "PROCEDURAL HISTORY",
        "TENTATIVE RULING",
        "MOTION",
        "OPPOSITION",
        "REPLY",
        "FINDINGS",
        "STATEMENT OF DECISION",
    }
)

# Indentation: 4+ spaces or a tab at the start of a line.
_INDENT_PATTERN: re.Pattern[str] = re.compile(r"^(?:    |\t)")

# Visual separator lines: lines composed entirely of underscores, dashes,
# equals signs, or asterisks (with optional whitespace).  These are used as
# section dividers in some court rulings and should be treated as paragraph
# breaks.  Require at least 3 repeated characters to avoid matching short
# dashes that might be part of content.
_SEPARATOR_PATTERN: re.Pattern[str] = re.compile(r"^\s*[_\-=*]{3,}\s*$")


def _is_section_header(line: str) -> bool:
    """Check if a line looks like an ALL CAPS section header."""
    stripped = line.strip()
    if not stripped:
        return False
    # Check known headers first (handles short ones like "ORDER")
    if stripped in _KNOWN_HEADERS:
        return True
    # For unknown headers, require at least 3 chars and all-caps pattern
    if len(stripped) >= 3 and _SECTION_HEADER_PATTERN.match(line):
        return True
    return False


def detect_paragraphs(text: str) -> str:
    """Detect paragraph boundaries in single-newline text.

    Inserts double-newline separators at detected paragraph boundaries.
    If the text already contains double newlines, it is returned as-is
    (the existing paragraph structure is assumed to be correct).

    Heuristics applied:
      - ALL CAPS section headers get a blank line inserted before them
      - Lines with significant indentation (4+ spaces or tab) after
        a non-indented line start a new paragraph
      - A short line (< 60% of average line length) followed by a line
        starting with a capital letter is treated as a paragraph boundary
    """
    if not text or "\n\n" in text:
        return text

    lines = text.split("\n")
    if len(lines) <= 1:
        return text

    # Compute average non-empty line length for short-line heuristic.
    # Exclude separator lines from the average calculation so they don't
    # skew the short-line heuristic.
    non_empty_lengths = [
        len(line) for line in lines if line.strip() and not _SEPARATOR_PATTERN.match(line)
    ]
    avg_len = sum(non_empty_lengths) / max(len(non_empty_lengths), 1)

    result: list[str] = []
    # Handle first line: if it is a separator, replace with empty line
    if _SEPARATOR_PATTERN.match(lines[0]):
        result.append("")
    else:
        result.append(lines[0])

    for i in range(1, len(lines)):
        current = lines[i]
        prev = lines[i - 1]

        # Heuristic 0: Separator lines (underscores, dashes, etc.) become
        # paragraph breaks.  The separator line itself is removed and replaced
        # with a blank line.
        if _SEPARATOR_PATTERN.match(current):
            result.append("")
            continue

        insert_break = False

        # Heuristic 1: Section headers in ALL CAPS
        if _is_section_header(current):
            insert_break = True

        # Heuristic 2: Indentation change (non-indented -> indented)
        elif _INDENT_PATTERN.match(current) and prev.strip() and not _INDENT_PATTERN.match(prev):
            insert_break = True

        # Heuristic 3: Short previous line + current starts with capital
        # Only trigger if previous line is meaningfully short and ends a sentence
        elif (
            prev.strip()
            and current.strip()
            and len(prev.rstrip()) < avg_len * 0.6
            and avg_len > 20
            and current.lstrip()[0:1].isupper()
            and prev.rstrip()[-1:] in {".", ":", ";", "!", "?"}
        ):
            insert_break = True

        if insert_break:
            result.append("")  # blank line = paragraph separator
        result.append(current)

    return "\n".join(result)


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
      4. Detect paragraph boundaries (insert double-newline separators)
      5. Collapse excessive whitespace

    The raw content in S3 is never modified — this only affects the
    ruling_text stored in Postgres for display.
    """
    if text is None:
        return None

    result = text
    result = fix_encoding(result)
    result = strip_page_numbers(result)
    result = strip_boilerplate(result)
    result = detect_paragraphs(result)
    result = collapse_whitespace(result)

    return result if result else None
