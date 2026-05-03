"""County-specific department name normalization.

Department names arrive from scrapers and LLM extraction with inconsistent
formatting.  This module normalizes them to canonical forms so that analytics
queries and department-level grouping work reliably.

Rules by county:

* **Los Angeles** — strip courtroom/calendar suffixes (`` #N``, ``N`` glued
  to single-letter depts), strip courthouse prefixes (``SSC-``), map
  ``SEP`` -> ``P``.  The single-letter+digits collapse (``X14`` → ``X``) is
  **skipped** for regional courthouses (Long Beach, Chatsworth, Antelope
  Valley) where the combined code identifies a distinct courtroom.  The
  carve-out can be triggered two ways:
    1. Pass ``courthouse=<name or code>`` — checked against
       ``_LA_LETTER_DIGITS_KEEP_COURTHOUSES``.
    2. Pass ``case_number=<number>`` — prefix checked against
       ``_LA_LETTER_DIGITS_KEEP_CASE_PREFIX_RE`` (covers LBCV/LBCP,
       CHCV/CHCP, AVCV/AVCP).  ``PC`` is intentionally omitted — it
       collides with LASC's old BC/PC format used across multiple
       courthouses.
* **Riverside** — strip leading zeros from purely numeric departments.
* **San Bernardino** — strip hyphens from letter+number codes
  (``S-17`` -> ``S17``).
* **Orange** — strip leading zeros from letter+number codes
  (``W08`` -> ``W8``, ``H01`` -> ``H1``).
* All other counties — pass through unchanged (after whitespace strip).

Called by the ingestion worker before DB writes so both scraper-provided
and LLM-extracted department values are normalized.

See: https://github.com/judgemind/judgemind/issues/2141
See: https://github.com/judgemind/judgemind/issues/4014
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# LA County
# ---------------------------------------------------------------------------

# Match " #N" suffix (courtroom/calendar number) at end of department string.
# Examples: "X #14" -> "X", "R #10" -> "R", "L #9" -> "L"
_LA_COURTROOM_SUFFIX_RE = re.compile(r"\s*#\d+$")

# Match a single letter followed by digits with no separator (e.g. "X14", "L10").
# These are department letter + courtroom number glued together.
_LA_LETTER_PLUS_DIGITS_RE = re.compile(r"^([A-Za-z])\d+$")

# Courthouse prefix "SSC-" (South San Clemente / Stanley Mosk sub-codes).
# Examples: "SSC-9" -> "9", "SSC-1" -> "1"
_LA_SSC_PREFIX_RE = re.compile(r"^SSC-(.+)$", re.IGNORECASE)

# Map known courthouse code department names to their canonical form.
# "SEP" is the Southeast courthouse "P" department.
_LA_COURTHOUSE_ALIASES: dict[str, str] = {
    "SEP": "P",
}

# Courthouse identifiers for regional LA courthouses where the letter+digits
# collapse must be skipped.  Values are case-folded for comparison.
#
# Long Beach: S25–S29 are distinct courtrooms.  Display name from
# ``_OPTION_TEXT_RE`` in ``la_tentatives.py``; ``LBC``/``LBCV`` are case-
# number prefixes included defensively.
#
# Chatsworth: F43–F51 are distinct courtrooms.  ``CHC`` is the courthouse
# code variant.
#
# Antelope Valley: A14 etc. are distinct courtrooms.  ``AV`` is the code.
_LA_LETTER_DIGITS_KEEP_COURTHOUSES: frozenset[str] = frozenset(
    {
        # Long Beach
        "long beach courthouse",
        "lbc",
        "lbcv",
        # Chatsworth
        "chatsworth courthouse north",
        "chc",
        # Antelope Valley
        "antelope valley courthouse",
        "av",
    }
)

# Case-number prefixes that indicate a regional courthouse where the
# letter+digits collapse must be skipped.
# - LBCV/LBCP → Long Beach Civil / Long Beach Complex
# - CHCV/CHCP → Chatsworth Civil / Chatsworth Complex
# - AVCV/AVCP → Antelope Valley Civil / Antelope Valley Complex
# ``PC`` is intentionally excluded — it collides with LASC old-format
# BC/PC numbers used across multiple courthouses (see llm_extractor.py:517).
_LA_LETTER_DIGITS_KEEP_CASE_PREFIX_RE = re.compile(
    r"^\d{2}(LBCV|LBCP|CHCV|CHCP|AVCV|AVCP)\d",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Riverside
# ---------------------------------------------------------------------------

# Leading zeros are stripped via int() conversion for purely numeric
# departments (see _normalize_riverside).

# ---------------------------------------------------------------------------
# San Bernardino
# ---------------------------------------------------------------------------

# Hyphen between letter and number: "S-17" -> "S17", "R-14" -> "R14".
_SB_HYPHEN_RE = re.compile(r"^([A-Za-z]+)-(\d+)$")

# ---------------------------------------------------------------------------
# Orange
# ---------------------------------------------------------------------------

# Leading zeros after a letter prefix: "W08" -> "W8", "H001" -> "H1".
# Anchored at start; replaces LETTERS+ZEROS+SIGNIFICANT_DIGIT prefix with
# LETTERS+SIGNIFICANT_DIGIT, leaving trailing characters intact.
_OC_LEADING_ZERO_RE = re.compile(r"^([A-Za-z]+)0+([1-9])")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_department(
    county: str,
    department: str | None,
    *,
    courthouse: str | None = None,
    case_number: str | None = None,
) -> str | None:
    """Normalize a department name using county-specific rules.

    Parameters
    ----------
    county : str
        The county name (e.g. ``"Los Angeles"``, ``"Riverside"``).
    department : str | None
        The raw department value from the scraper or LLM.
    courthouse : str | None
        Optional courthouse display name or code.  Used by the LA normalizer
        to skip the letter+digits collapse for regional courthouses (Long
        Beach, Chatsworth, Antelope Valley) where the combined code
        identifies a distinct courtroom.
    case_number : str | None
        Optional case number.  Used by the LA normalizer as a fallback when
        ``courthouse`` is absent — a LBCV/CHCV/AVCV prefix triggers the
        same letter+digits keep rule.  ``PC`` is intentionally omitted from
        the prefix set (collision with LASC old-format BC/PC numbers).

    Returns
    -------
    str | None
        The normalized department string, or ``None`` if the input was
        ``None`` or empty after stripping.
    """
    if department is None:
        return None

    dept = department.strip()
    if not dept:
        return None

    county_lower = county.lower()

    if county_lower == "los angeles":
        dept = _normalize_la(dept, courthouse=courthouse, case_number=case_number)
    elif county_lower == "riverside":
        dept = _normalize_riverside(dept)
    elif county_lower == "san bernardino":
        dept = _normalize_san_bernardino(dept)
    elif county_lower == "orange":
        dept = _normalize_orange(dept)

    return dept if dept else None


def _normalize_la(
    dept: str,
    *,
    courthouse: str | None = None,
    case_number: str | None = None,
) -> str:
    """Normalize an LA County department name.

    Transformations (applied in order):

    1. Strip ``SSC-`` courthouse prefix (reveals underlying pattern).
    2. Map known courthouse aliases (``SEP`` -> ``P``).
    3. Strip `` #N`` courtroom/calendar suffix.
    4. Strip single-letter + digits glue (``X14`` -> ``X``), **unless**
       the department is from a regional courthouse (Long Beach, Chatsworth,
       Antelope Valley) where the combined code identifies a distinct
       courtroom.  The carve-out fires when either:
       * ``courthouse`` (case-folded) is in
         ``_LA_LETTER_DIGITS_KEEP_COURTHOUSES``, or
       * ``case_number`` matches ``_LA_LETTER_DIGITS_KEEP_CASE_PREFIX_RE``
         (LBCV/LBCP/CHCV/CHCP/AVCV/AVCP prefixes).
    """
    # 1. Strip SSC- prefix first to reveal underlying patterns
    m = _LA_SSC_PREFIX_RE.match(dept)
    if m:
        dept = m.group(1)

    # 2. Courthouse aliases
    dept_upper = dept.upper()
    if dept_upper in _LA_COURTHOUSE_ALIASES:
        dept = _LA_COURTHOUSE_ALIASES[dept_upper]

    # 3. Strip " #N" suffix
    dept = _LA_COURTROOM_SUFFIX_RE.sub("", dept).strip()

    # 4. Single letter + digits -> letter only.
    # Skip when the courthouse or case_number indicates a regional courthouse
    # where letter+digit codes are meaningful identifiers.
    courthouse_keep = (courthouse or "").strip().lower() in _LA_LETTER_DIGITS_KEEP_COURTHOUSES
    case_number_keep = bool(
        case_number and _LA_LETTER_DIGITS_KEEP_CASE_PREFIX_RE.match(case_number)
    )
    keep_letter_digits = courthouse_keep or case_number_keep
    if not keep_letter_digits:
        m = _LA_LETTER_PLUS_DIGITS_RE.match(dept)
        if m:
            dept = m.group(1)

    return dept


def _normalize_riverside(dept: str) -> str:
    """Normalize a Riverside County department name.

    Strips leading zeros from purely numeric departments.
    Handles edge cases like ``"00"`` -> ``"0"``.
    """
    if dept.isdigit():
        return str(int(dept))
    return dept


def _normalize_san_bernardino(dept: str) -> str:
    """Normalize a San Bernardino County department name.

    Strips hyphens from letter+number codes (``S-17`` -> ``S17``).
    """
    m = _SB_HYPHEN_RE.match(dept)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return dept


def _normalize_orange(dept: str) -> str:
    """Normalize an Orange County department name.

    Strips leading zeros from letter+number codes (``W08`` -> ``W8``,
    ``H001`` -> ``H1``).  Trailing characters after the significant digit
    are preserved (``H012`` -> ``H12``, ``L0612`` -> ``L612``).

    Input is uppercased before applying the regex so that lowercase and
    mixed-case codes (``w08``, ``Cm01``) are normalised to their canonical
    uppercase form (``W8``, ``CM1``).
    """
    dept = dept.upper()
    return _OC_LEADING_ZERO_RE.sub(r"\1\2", dept)
