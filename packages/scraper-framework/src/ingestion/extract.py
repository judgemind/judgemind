"""Basic regex-based extraction of outcome and motion_type from ruling text.

This provides a lightweight, zero-cost fallback when scrapers do not populate
outcome/motion_type in the event payload. For higher-accuracy classification,
see the NLP pipeline's RulingClassifier (packages/nlp-pipeline).

The regex patterns target common California tentative ruling language.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Outcome extraction
# ---------------------------------------------------------------------------

# Ordered so more specific patterns match first (e.g. "granted in part"
# before "granted"). Each tuple is (pattern, outcome_value).
_OUTCOME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgranted\s+in\s+part\b", re.IGNORECASE), "granted_in_part"),
    (re.compile(r"\bdenied\s+in\s+part\b", re.IGNORECASE), "denied_in_part"),
    (re.compile(r"\bgranted\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bdenied\b", re.IGNORECASE), "denied"),
    (re.compile(r"\bmoot\b", re.IGNORECASE), "moot"),
    (re.compile(r"\bcontinued\b", re.IGNORECASE), "continued"),
    (re.compile(r"\boff[\s-]?calendar\b", re.IGNORECASE), "off_calendar"),
    (re.compile(r"\bsubmitted\b", re.IGNORECASE), "submitted"),
]

# ---------------------------------------------------------------------------
# Motion type extraction
# ---------------------------------------------------------------------------

# Ordered so more specific patterns match first (e.g. "summary adjudication"
# before "summary judgment").
_MOTION_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(?:motion\s+for\s+)?summary\s+adjudication\b",
            re.IGNORECASE,
        ),
        "msj_partial",
    ),
    (
        re.compile(
            r"\bpartial\s+summary\s+judgment\b",
            re.IGNORECASE,
        ),
        "msj_partial",
    ),
    (
        re.compile(
            r"\b(?:motion\s+for\s+)?summary\s+judgment\b",
            re.IGNORECASE,
        ),
        "msj",
    ),
    (re.compile(r"\bmotion\s+to\s+dismiss\b", re.IGNORECASE), "mtd"),
    (re.compile(r"\bmotion\s+in\s+limine\b", re.IGNORECASE), "mil"),
    (re.compile(r"\bdemurrer\b", re.IGNORECASE), "demurrer"),
    (re.compile(r"\bmotion\s+to\s+compel\b", re.IGNORECASE), "motion_to_compel"),
    (
        re.compile(r"\banti[- ]?slapp\b", re.IGNORECASE),
        "anti_slapp",
    ),
    (re.compile(r"\bmotion\s+to\s+strike\b", re.IGNORECASE), "motion_to_strike"),
    (
        re.compile(r"\bpreliminary\s+injunction\b", re.IGNORECASE),
        "preliminary_injunction",
    ),
]


def extract_outcome(ruling_text: str) -> str | None:
    """Extract a ruling outcome from text using regex patterns.

    Returns the first matching outcome value (from the ``ruling_outcome``
    PostgreSQL enum), or ``None`` if no pattern matches.
    """
    for pattern, value in _OUTCOME_PATTERNS:
        if pattern.search(ruling_text):
            return value
    return None


def extract_motion_type(ruling_text: str) -> str | None:
    """Extract a motion type from text using regex patterns.

    Returns the first matching motion type value, or ``None`` if no
    pattern matches.
    """
    for pattern, value in _MOTION_TYPE_PATTERNS:
        if pattern.search(ruling_text):
            return value
    return None


# ---------------------------------------------------------------------------
# Judge name extraction
# ---------------------------------------------------------------------------

# Patterns drawn from the California court scrapers.  Each targets a
# different court's formatting style so the backfill can recover judge
# names from ruling text that was already stored in the database.

_JUDGE_NAME_PATTERNS: list[re.Pattern[str]] = [
    # LA: "William A. Crowfoot Judge of the Superior Court"
    re.compile(r"([^\n]+?)\s+Judge of the Superior Court"),
    # SB: "Department S22 - Judge Bobby P. Luna"
    re.compile(
        r"Department\s+\S+?\s*[-\u2013\u2014]\s*Judge\s+(?P<judge_name>[^\n]+)",
        re.IGNORECASE,
    ),
    # SB alternate: "BEFORE THE HONORABLE BOBBY P. LUNA"
    re.compile(r"BEFORE THE HONORABLE\s+(?P<judge_name>[^\n]+)", re.IGNORECASE),
    # SF: "Presiding: BOBBY P. LUNA"
    re.compile(r"Presiding:\s+(?P<judge_name>[A-Z][^\n]+)"),
    # Riverside / OC style: "Department 1 - Honorable John A. Smith"
    re.compile(
        r"Department\s+\S+\s*-\s*Honorable\s+(?P<judge_name>[^\n]+)",
        re.IGNORECASE,
    ),
]


def extract_judge_name(ruling_text: str) -> str | None:
    """Extract a judge name from ruling text using court-specific regex patterns.

    Tries multiple patterns used by California court scrapers (LA, SB, SF,
    Riverside, OC).  Returns the first matched name stripped of whitespace,
    or ``None`` if no pattern matches.

    The returned name is *raw* — callers should pass it through
    ``normalize_judge_name`` before using it as a canonical name.
    """
    for pattern in _JUDGE_NAME_PATTERNS:
        m = pattern.search(ruling_text)
        if m:
            # Use named group 'judge_name' if present, otherwise group 1
            try:
                name = m.group("judge_name")
            except IndexError:
                name = m.group(1)
            name = " ".join(name.strip().split())  # collapse whitespace
            if name:
                return name
    return None
