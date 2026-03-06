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
