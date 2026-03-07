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
    # --- New patterns added for issue #260 ---
    (
        re.compile(r"\bex\s+parte\s+application\b", re.IGNORECASE),
        "ex_parte_application",
    ),
    (
        re.compile(r"\bex\s+parte\s+motion\b", re.IGNORECASE),
        "ex_parte_application",
    ),
    (
        re.compile(
            r"\bpetition\s+for\s+writ\s+of\s+(?:mandate|mandamus)\b",
            re.IGNORECASE,
        ),
        "petition_writ_of_mandate",
    ),
    (
        re.compile(
            r"\bpetition\s+for\s+writ\s+of\s+habeas\s+corpus\b",
            re.IGNORECASE,
        ),
        "petition_habeas_corpus",
    ),
    (
        re.compile(r"\bpetition\b", re.IGNORECASE),
        "petition",
    ),
    (
        re.compile(r"\border\s+to\s+show\s+cause\b", re.IGNORECASE),
        "osc",
    ),
    (
        re.compile(r"\bmotion\s+to\s+quash\b", re.IGNORECASE),
        "motion_to_quash",
    ),
    (
        re.compile(r"\bmotion\s+for\s+reconsideration\b", re.IGNORECASE),
        "motion_for_reconsideration",
    ),
    (
        re.compile(r"\bmotion\s+for\s+protective\s+order\b", re.IGNORECASE),
        "motion_for_protective_order",
    ),
    (
        re.compile(r"\bmotion\s+for\s+attorney.?s?\s+fees\b", re.IGNORECASE),
        "motion_for_attorney_fees",
    ),
    (
        re.compile(
            r"\bmotion\s+to\s+set\s+aside\s+(?:the\s+)?default\b",
            re.IGNORECASE,
        ),
        "motion_to_set_aside_default",
    ),
    (
        re.compile(r"\bmotion\s+to\s+vacate\b", re.IGNORECASE),
        "motion_to_vacate",
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
    # LA: "William A. Crowfoot Judge of the Superior Court" (now case-insensitive
    # to also match "JARED D. MOSES JUDGE OF THE SUPERIOR COURT")
    re.compile(
        r"([^\n]+?)\s+Judge of the Superior Court",
        re.IGNORECASE,
    ),
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
    # "JUDICIAL OFFICER: John A. Smith" — used by some LA and inland courts.
    # Captures everything to end of line after the colon.
    re.compile(
        r"JUDICIAL\s+OFFICER\s*:\s*(?P<judge_name>[A-Za-z][^\n]+)",
        re.IGNORECASE,
    ),
    # "Hon. John A. Smith" or "Honorable John A. Smith" (standalone, many courts).
    # Requires first+last name minimum.  Uses literal spaces (not \s) so names
    # do not span across newlines.  Supports hyphenated surnames.
    re.compile(
        r"\bHon(?:orable)?\.?[ ]+"
        r"(?P<judge_name>"
        r"[A-Z][a-z]+"  # first name
        r"(?:[ ]+[A-Z][a-z.'\-]*)*"  # middle initials/names
        r"[ ]+[A-Z][a-z]+(?:-[A-Z][a-z]+)*"  # last name (optionally hyphenated)
        r")",
    ),
    # Standalone "Judge: Name" or "Judge Name" in headers.  Same name-shape
    # constraints as "Hon." pattern.  Uses literal spaces to stay on one line.
    re.compile(
        r"(?<![a-zA-Z])"  # not preceded by a letter
        r"Judge[:  ][ ]*"
        r"(?P<judge_name>"
        r"[A-Z][a-z]+"  # first name
        r"(?:[ ]+[A-Z][a-z.'\-]*)*"  # middle initials/names
        r"[ ]+[A-Z][a-z]+(?:-[A-Z][a-z]+)*"  # last name (optionally hyphenated)
        r")"
        r"(?![ ]+of\b)",  # exclude "Judge X of the Superior Court"
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
