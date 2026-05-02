"""County-specific department name normalization.

Department names arrive from scrapers and LLM extraction with inconsistent
formatting.  This module normalizes them to canonical forms so that analytics
queries and department-level grouping work reliably.

Rules by county:

* **Los Angeles** — strip courtroom/calendar suffixes (`` #N``, ``N`` glued
  to single-letter depts), strip courthouse prefixes (``SSC-``), map
  ``SEP`` -> ``P``.
* **Riverside** — strip leading zeros from purely numeric departments.
* **San Bernardino** — strip hyphens from letter+number codes
  (``S-17`` -> ``S17``).
* **Orange** — strip leading zeros from letter+number codes
  (``W08`` -> ``W8``, ``H01`` -> ``H1``).
* All other counties — pass through unchanged (after whitespace strip).

Called by the ingestion worker before DB writes so both scraper-provided
and LLM-extracted department values are normalized.

See: https://github.com/judgemind/judgemind/issues/2141
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

# Long Beach Courthouse identifiers (case-folded).
# S25–S29 are distinct courtrooms at LB; we skip the letter+digits collapse
# so these codes survive normalization intact.  The display name comes from
# ``_OPTION_TEXT_RE`` in ``la_tentatives.py``; ``LBC``/``LBCV`` are the
# case-number prefixes — included defensively in case they ever appear in the
# event payload.
_LA_LB_COURTHOUSE_NAMES: frozenset[str] = frozenset({"long beach courthouse", "lbc", "lbcv"})

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
_OC_LEADING_ZERO_RE = re.compile(r"^([A-Z]+)0+([1-9])")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_department(
    county: str,
    department: str | None,
    *,
    courthouse: str | None = None,
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
        to skip the letter+digits collapse for courthouses (e.g. Long Beach)
        where the combined code is a real identifier, not a glued suffix.

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
        dept = _normalize_la(dept, courthouse=courthouse)
    elif county_lower == "riverside":
        dept = _normalize_riverside(dept)
    elif county_lower == "san bernardino":
        dept = _normalize_san_bernardino(dept)
    elif county_lower == "orange":
        dept = _normalize_orange(dept)

    return dept if dept else None


def _normalize_la(dept: str, *, courthouse: str | None = None) -> str:
    """Normalize an LA County department name.

    Transformations (applied in order):

    1. Strip ``SSC-`` courthouse prefix (reveals underlying pattern).
    2. Map known courthouse aliases (``SEP`` -> ``P``).
    3. Strip `` #N`` courtroom/calendar suffix.
    4. Strip single-letter + digits glue (``X14`` -> ``X``), **unless**
       the courthouse is a Long Beach courthouse — at LB these combined
       codes (``S25``–``S29``) identify distinct courtrooms and must be
       preserved.
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
    # Skip for Long Beach Courthouse where the combined code is meaningful.
    is_lb = (courthouse or "").strip().lower() in _LA_LB_COURTHOUSE_NAMES
    if not is_lb:
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
    """
    return _OC_LEADING_ZERO_RE.sub(r"\1\2", dept)
