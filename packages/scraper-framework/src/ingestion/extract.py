"""Basic regex-based extraction of outcome and motion_type from ruling text.

This provides a lightweight, zero-cost fallback when scrapers do not populate
outcome/motion_type in the event payload. For higher-accuracy classification,
see the NLP pipeline's RulingClassifier (packages/nlp-pipeline).

The regex patterns target common California tentative ruling language.
"""

from __future__ import annotations

import re
from datetime import date, datetime

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
    (re.compile(r"\bmotions?\s+to\s+compel\b", re.IGNORECASE), "motion_to_compel"),
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
        re.compile(
            r"\bclass\s+action\s+settlement\b|\bpreliminary\s+approval\b",
            re.IGNORECASE,
        ),
        "class_action_settlement",
    ),
    # --- New patterns added for issue #1767 (probate/non-standard event types) ---
    (
        re.compile(
            r"\bpetition\s+(?:for\s+)?(?:probate|to\s+administer\s+estate"
            r"|for\s+letters)\b",
            re.IGNORECASE,
        ),
        "petition_for_probate",
    ),
    (
        re.compile(
            r"\b(?:guardianship\s+petition|petition\s+for\s+"
            r"(?:guardianship|conservatorship))\b",
            re.IGNORECASE,
        ),
        "guardianship_petition",
    ),
    (
        re.compile(r"\baccounting\b", re.IGNORECASE),
        "accounting",
    ),
    (
        re.compile(r"\bshow\s+cause\s+hearing\b", re.IGNORECASE),
        "show_cause_hearing",
    ),
    (
        re.compile(r"\btrust\s+petition\b", re.IGNORECASE),
        "trust_petition",
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
        re.compile(
            r"\bmotion\s+for\s+attorney['\u2018\u2019]?s?['\u2018\u2019]?\s*fees\b",
            re.IGNORECASE,
        ),
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
    # --- New patterns added for issue #421 ---
    (
        re.compile(r"\bdefault\s+judgment\b", re.IGNORECASE),
        "default_judgment",
    ),
    (
        re.compile(r"\bto\s+be\s+relieved\s+as\s+counsel\b", re.IGNORECASE),
        "motion_to_be_relieved_as_counsel",
    ),
    (
        re.compile(r"\bmotion\s+for\s+leave\b", re.IGNORECASE),
        "motion_for_leave_to_amend",
    ),
    (
        re.compile(r"\bmotion\s+for\s+sanctions\b", re.IGNORECASE),
        "motion_for_sanctions",
    ),
    (
        re.compile(r"\bmotion\s+for\s+relief\b", re.IGNORECASE),
        "motion_for_relief",
    ),
    (
        re.compile(r"\bmotion\s+for\s+pro\s+hac\s+vice\b", re.IGNORECASE),
        "motion_pro_hac_vice",
    ),
    (
        re.compile(r"\bmotion\s+to\s+substitute\b", re.IGNORECASE),
        "motion_to_substitute",
    ),
    (
        re.compile(r"\bMILs?\b"),
        "mil",
    ),
    (
        re.compile(r"\bmotion\s+to\s+tax\s+costs\b", re.IGNORECASE),
        "motion_to_tax_costs",
    ),
    (
        re.compile(r"\bwrit\s+of\s+possession\b", re.IGNORECASE),
        "writ_of_possession",
    ),
    (
        re.compile(r"\bmotion\s+for\s+new\s+trial\b", re.IGNORECASE),
        "motion_for_new_trial",
    ),
    # --- New patterns added for issue #1783 ---
    (
        re.compile(
            r"\bmotion\s+for\s+judgment\s+on\s+the\s+pleadings\b",
            re.IGNORECASE,
        ),
        "motion_for_judgment_on_the_pleadings",
    ),
    (
        re.compile(
            r"\bmotion\s+to\s+deem\b.*\badmissions?\s+admitted\b",
            re.IGNORECASE,
        ),
        "deem_admissions_admitted",
    ),
    (
        re.compile(r"\bmotion\s+to\s+deem\s+requests?\b", re.IGNORECASE),
        "deem_admissions_admitted",
    ),
    # Broad ex parte fallback — must come after specific ex_parte_application/motion
    (
        re.compile(r"\bex\s+parte\b", re.IGNORECASE),
        "ex_parte_application",
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


# Set of all known normalized (snake_case) motion type values.
_KNOWN_MOTION_TYPES: frozenset[str] = frozenset(value for _, value in _MOTION_TYPE_PATTERNS)


# ---------------------------------------------------------------------------
# Prefix-less motion type patterns (#1783)
# ---------------------------------------------------------------------------
# Some scrapers (notably Riverside) produce motion type descriptions without
# the "motion to/for" prefix — e.g. "Attorney's Fees" instead of "Motion for
# Attorney's Fees", or "Compel Plaintiff's Responses" instead of "Motion to
# Compel Plaintiff's Responses".  These patterns are used ONLY by
# normalize_motion_type() as a fallback after the standard
# extract_motion_type() patterns fail.  They are intentionally broader than
# _MOTION_TYPE_PATTERNS because false positives are unlikely in scraper
# metadata (short strings) whereas they would be problematic in full ruling
# text.

_PREFIX_LESS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Order: more specific patterns before broader ones.
    (
        re.compile(r"\battorney['\u2018\u2019]?s?['\u2018\u2019]?\s*fees?\b", re.IGNORECASE),
        "motion_for_attorney_fees",
    ),
    (re.compile(r"\bnew\s+trial\b", re.IGNORECASE), "motion_for_new_trial"),
    (
        re.compile(r"\bjudgment\s+on\s+the\s+pleadings\b", re.IGNORECASE),
        "motion_for_judgment_on_the_pleadings",
    ),
    (
        re.compile(r"\bdeem\b.*\badmissions?\s+admitted\b", re.IGNORECASE),
        "deem_admissions_admitted",
    ),
    (re.compile(r"\bdeem\s+requests?\b", re.IGNORECASE), "deem_admissions_admitted"),
    (
        re.compile(r"\bterminating\s+sanctions?\b", re.IGNORECASE),
        "motion_for_sanctions",
    ),
    (re.compile(r"\bterminating\b", re.IGNORECASE), "motion_for_sanctions"),
    (
        re.compile(r"\bmonetary\s+sanctions?\b", re.IGNORECASE),
        "motion_for_sanctions",
    ),
    (re.compile(r"\bcompel\b", re.IGNORECASE), "motion_to_compel"),
    (
        re.compile(r"\bproduction\s+of\s+documents?\b", re.IGNORECASE),
        "motion_to_compel",
    ),
    (re.compile(r"\bprotective\s+order\b", re.IGNORECASE), "motion_for_protective_order"),
    (re.compile(r"\brelief\s+from\s+default\b", re.IGNORECASE), "motion_to_set_aside_default"),
    (re.compile(r"\bleave\s+to\s+amend\b", re.IGNORECASE), "motion_for_leave_to_amend"),
    (re.compile(r"\bstrike\b", re.IGNORECASE), "motion_to_strike"),
    (re.compile(r"\bquash\b", re.IGNORECASE), "motion_to_quash"),
    (re.compile(r"\breconsideration\b", re.IGNORECASE), "motion_for_reconsideration"),
    (re.compile(r"\bpro\s+hac\s+vice\b", re.IGNORECASE), "motion_pro_hac_vice"),
    (re.compile(r"\btax\s+costs\b", re.IGNORECASE), "motion_to_tax_costs"),
    (re.compile(r"\bvacate\b", re.IGNORECASE), "motion_to_vacate"),
    # Broad sanctions fallback — must come after more specific standalone patterns
    # to avoid shadowing "strike", "compel", "quash", etc. when the input
    # contains multiple keywords (e.g. "Strike and Sanctions").
    (re.compile(r"\bsanctions?\b", re.IGNORECASE), "motion_for_sanctions"),
]


def normalize_motion_type(motion_type: str) -> str | None:
    """Normalize a motion type string to its canonical snake_case form.

    Scrapers may produce motion types in various formats — title case
    (e.g. ``"Motion to Compel"``), calendar event names
    (e.g. ``"Motion Hearing"``), or composite types
    (e.g. ``"Demurrer/Motion to Strike"``).  Some scrapers (notably
    Riverside) also produce prefix-less descriptions like
    ``"Attorney's Fees"`` or ``"Compel Plaintiff's Responses"``.  This
    function converts any such value to the canonical snake_case form
    used by the enrichment pipeline and ``_MOTION_TYPE_CASE_TYPE_MAP``.

    Parameters
    ----------
    motion_type : str
        The raw motion type value from a scraper or event payload.

    Returns
    -------
    str | None
        The normalized snake_case value (e.g. ``"motion_to_compel"``),
        or ``None`` if the value cannot be mapped to a known type.
        Returning ``None`` allows the caller to fall back to regex
        extraction from ruling text.
    """
    if not motion_type:
        return None

    stripped = motion_type.strip()
    if not stripped:
        return None

    # Already normalized — return as-is.
    if stripped in _KNOWN_MOTION_TYPES:
        return stripped

    # Try matching against the regex patterns.  This handles title-case
    # values like "Motion to Compel Further Responses" or "Demurrer to
    # Complaint" as well as composite calendar event types like
    # "Demurrer/Motion to Strike" (the first matching pattern wins).
    matched = extract_motion_type(stripped)
    if matched is not None:
        return matched

    # Try prefix-less patterns (#1783).  These are broader patterns that
    # handle scraper metadata missing the "motion to/for" prefix — e.g.
    # "Attorney's Fees", "Compel Plaintiff's Responses", "New Trial".
    for pattern, value in _PREFIX_LESS_PATTERNS:
        if pattern.search(stripped):
            return value

    # No known mapping — return None so the caller can fall back to
    # ruling text extraction.
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
    # LA ALL-CAPS header: "DEPARTMENT 56 JUDGE STEVEN A. ELLIS" (#401)
    # Matches "JUDGE FIRST [M.] LAST" in ALL-CAPS, typically preceded by
    # "DEPARTMENT <number>".  Requires at least first + last name in uppercase.
    # Case-sensitive: only matches ALL-CAPS names to avoid false positives on
    # phrases like "judge decided the case".
    re.compile(
        r"(?:DEPARTMENT\s+\S+\s+)?"
        r"JUDGE\s+"
        r"(?P<judge_name>"
        r"[A-Z]{2,}"  # first name (all caps, 2+ chars)
        r"(?:\s+[A-Z]\.?)*"  # optional middle initials
        r"\s+[A-Z]{2,}"  # last name (all caps, 2+ chars)
        r"(?:-[A-Z]{2,})?"  # optional hyphenated surname
        r")"
        r"(?!\s+[Oo][Ff]\b)",  # exclude "JUDGE X OF the Superior Court"
    ),
]


# ---------------------------------------------------------------------------
# Case number extraction
# ---------------------------------------------------------------------------

# Patterns drawn from the California court scrapers.  Ordered so the most
# specific (and least likely to false-positive) patterns match first.
# Each pattern is compiled once at module load time.

_CASE_NUMBER_PATTERNS: list[re.Pattern[str]] = [
    # LA label pattern: "Case Number: 24NNCV02551" — most reliable when present.
    # The label anchors the match so the captured group can be broad.
    # Uses [\w-]+ to also capture hyphenated case numbers like FPT-25-378624.
    re.compile(r"Case\s+Number\s*:\s*([\w-]+)", re.IGNORECASE),
    # San Francisco: F + 2 letters + hyphen + 2-digit year + hyphen + 6 digits.
    # e.g. FPT-25-378624, FMS-20-387302, FDI-14-781786
    re.compile(r"\bF[A-Z]{2}-\d{2}-\d{6}\b"),
    # San Bernardino: CIV + 2 letters + optional space + 5-8 digits.
    # e.g. CIVRS2502080, CIVSB2416631, CIVSB 2600093 (Dept S36 uses a space).
    # Callers should normalise by removing any internal whitespace.
    re.compile(r"\bCIV[A-Z]{2}\s*\d{5,8}\b"),
    # Riverside: CV + 2-4 letters + 6-8 digits. e.g. CVPS2306157
    re.compile(r"\bCV[A-Z]{2,4}\d{6,8}\b"),
    # Santa Clara: 2-digit year + CV + 6 digits. e.g. 24CV443183
    re.compile(r"\b\d{2}CV\d{6}\b"),
    # LA standalone: 2-digit year + 2-letter area code + 2-letter case type + 5-6 digits.
    # Area codes: NN, ST, AV, SC, VE, BB, PD, GC, NE, WE, LC, CC, etc.
    # Case types: CV (civil), CP (complex/probate), LC (limited civil), etc.
    # e.g. 24NNCV02551, 26NNCP00062, 23STCV12345
    re.compile(r"\b\d{2}[A-Z]{2}(?:CV|CP|LC|CC|BB|PD|GC|NE|WE)\d{5,6}\b"),
    # OC Civil: 2-4 digit prefix + hyphen + 8 digits. e.g. 30-2024-01370288
    re.compile(r"\b\d{2,4}-\d{8}\b"),
    # OC Family Law: 2-digit year + D + 6 digits. e.g. 24D006789
    re.compile(r"\b\d{2}D\d{6}\b"),
]


def extract_case_number(ruling_text: str) -> str | None:
    """Extract a case number from ruling text using court-specific regex patterns.

    Tries multiple patterns used by California court scrapers (LA, SF, SB,
    Riverside, SC, OC).  Returns the first matched case number, or ``None``
    if no pattern matches.

    This is a fallback for when the scraper does not populate ``case_number``
    in the event payload.  The patterns are ordered so more specific formats
    match first to minimize false positives.
    """
    for pattern in _CASE_NUMBER_PATTERNS:
        m = pattern.search(ruling_text)
        if m:
            # Use group(1) if there's a capture group, otherwise group(0)
            if m.lastindex and m.lastindex >= 1:
                raw = m.group(1)
            else:
                raw = m.group(0)
            # Normalise: remove internal whitespace (e.g. "CIVSB 2600093" →
            # "CIVSB2600093" for SB Dept S36 format).
            return raw.replace(" ", "")
    return None


# ---------------------------------------------------------------------------
# Case type extraction from case number prefix
# ---------------------------------------------------------------------------

# Ordered so more specific prefixes match first.  Each tuple is
# (compiled regex, case_type value).  Patterns are anchored to the start
# of the case number (after optional leading digits for court/year codes).
_CASE_TYPE_PREFIX_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Civil prefixes — CV variants (Riverside, Santa Clara, SB, LA)
    # Matches: CVRI, CVME, CVPS, CVSW, CVCO, 24CV, 24NNCV, 23STCV, CIV*, etc.
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?CV", re.IGNORECASE), "civil"),
    (re.compile(r"^CIV", re.IGNORECASE), "civil"),
    # Ventura civil codes: CU (Civil Unlimited), CL (Civil Limited)
    # Standard format: 4-digit year + CU/CL + 2-letter subtype + 6 digits
    # e.g. 2025CUBC042098, 2024CLCL035410
    # Older format: longer digit prefix (e.g. 202200570068CUMM)
    (re.compile(r"^\d{4,}CU", re.IGNORECASE), "civil"),
    (re.compile(r"^\d{4,}CL[A-Z]", re.IGNORECASE), "civil"),
    # Family law prefixes
    (re.compile(r"^(?:\d{2,4})?FL", re.IGNORECASE), "family"),
    (re.compile(r"^(?:\d{2,4})?DV", re.IGNORECASE), "family"),
    (re.compile(r"^(?:\d{2,4})?D\d", re.IGNORECASE), "family"),
    # Probate prefixes
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?PR", re.IGNORECASE), "probate"),
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?BP", re.IGNORECASE), "probate"),
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?CP", re.IGNORECASE), "probate"),
    # Small claims
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?SC", re.IGNORECASE), "small_claims"),
    # Criminal prefixes
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?CR", re.IGNORECASE), "criminal"),
    # Felony docket format: F + digit (e.g. F2301234)
    (re.compile(r"^F\d", re.IGNORECASE), "criminal"),
    # SF felony: FPT, FMS, FDI, etc. — F + 2 letters + hyphen
    (re.compile(r"^F[A-Z]{2}-", re.IGNORECASE), "criminal"),
    # Juvenile
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?JV", re.IGNORECASE), "juvenile"),
    # Traffic
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?TR", re.IGNORECASE), "traffic"),
    # OC civil format: digits-digits (e.g. 30-2024-01370288 or 2024-01380242)
    (re.compile(r"^\d{2,4}-\d{4,8}"), "civil"),
]


def is_valid_case_number(value: str) -> bool:
    """Return True if *value* looks like a valid case number, not a case title.

    Rejects values that contain adversarial party indicators ("vs", "v."),
    legal citation patterns, or prose text.  These are common false positives
    from LLM extraction on documents that lack formal case numbers (e.g. OC
    North Justice Center PDFs).

    Examples of invalid "case numbers" this catches:
        - "Smith v. Kia" (case title)
        - "Catalan v. FCA" (case title)
        - "Cal. App. 4th 1, 20; DuPont Merck" (legal citation)
        - "Tuinenburg v. Before the Court is a demurrer" (case title + ruling text)
    """
    if not value or not value.strip():
        return False
    stripped = value.strip()
    # Reject if it contains "vs", "vs.", or "v." — this is a case title
    if re.search(r"\bvs?\.?\s", stripped, re.IGNORECASE):
        return False
    # Reject if it contains ruling text indicators
    if re.search(
        r"\b(?:the court|motion|demurrer|granted|denied|hearing|ruling|"
        r"tentative|calendar|petition|application)\b",
        stripped,
        re.IGNORECASE,
    ):
        return False
    # Reject legal citations (e.g. "Cal. App. 4th 1, 20")
    if re.search(r"\bCal\.\s*App\b|\bF\.\s*Supp\b|\bF\.\s*\d", stripped, re.IGNORECASE):
        return False
    # Reject if it's very long (case numbers are short, titles are long)
    if len(stripped) > 30:
        return False
    return True


# ---------------------------------------------------------------------------
# Case type extraction from scraper ID
# ---------------------------------------------------------------------------

# Maps scraper_id substrings to case_type values.  The scraper_id encodes
# the court and case category (e.g. "ca-oc-tentatives-civil" -> civil).
_SCRAPER_ID_CASE_TYPE_MAP: list[tuple[str, str]] = [
    ("civil", "civil"),
    ("family", "family"),
    ("probate", "probate"),
    ("criminal", "criminal"),
    ("small-claims", "small_claims"),
    ("juvenile", "juvenile"),
    ("traffic", "traffic"),
]


def extract_case_type_from_scraper_id(scraper_id: str) -> str | None:
    """Infer case type from a scraper_id string.

    This is a zero-cost fallback when neither the LLM nor the case number
    prefix yields a case_type.  Scraper IDs encode the case category in
    their suffix (e.g. ``ca-oc-tentatives-civil``).

    Returns one of the case type strings, or ``None`` if the scraper_id
    does not contain a recognized case type indicator.
    """
    if not scraper_id:
        return None
    scraper_lower = scraper_id.lower()
    for keyword, case_type in _SCRAPER_ID_CASE_TYPE_MAP:
        if keyword in scraper_lower:
            return case_type
    return None


# ---------------------------------------------------------------------------
# Case type extraction from motion type (#1702)
# ---------------------------------------------------------------------------

# Maps normalized motion_type values to case_type values.  This is a final
# fallback when case number prefix and scraper_id fail to determine the type.
# Only motion types that unambiguously identify a case type are included.
_MOTION_TYPE_CASE_TYPE_MAP: dict[str, str] = {
    # Civil motion types
    "msj": "civil",
    "msj_partial": "civil",
    "mtd": "civil",
    "demurrer": "civil",
    "motion_to_compel": "civil",
    "anti_slapp": "civil",
    "motion_to_strike": "civil",
    "preliminary_injunction": "civil",
    "motion_for_protective_order": "civil",
    "motion_for_attorney_fees": "civil",
    "motion_to_set_aside_default": "civil",
    "motion_to_vacate": "civil",
    "default_judgment": "civil",
    "motion_to_be_relieved_as_counsel": "civil",
    "motion_for_leave_to_amend": "civil",
    "motion_for_sanctions": "civil",
    "motion_for_relief": "civil",
    "motion_pro_hac_vice": "civil",
    "motion_to_substitute": "civil",
    "motion_to_tax_costs": "civil",
    "motion_for_new_trial": "civil",
    "motion_for_reconsideration": "civil",
    "motion_to_quash": "civil",
    "motion_for_judgment_on_the_pleadings": "civil",
    "deem_admissions_admitted": "civil",
    "class_action_settlement": "civil",
    "writ_of_possession": "civil",
    "mil": "civil",
    # Probate-specific motion types
    "petition": "probate",
    "petition_for_probate": "probate",
    "guardianship_petition": "probate",
    "trust_petition": "probate",
    # Accounting and show cause hearing can appear in multiple case types — excluded.
    # Ex parte and OSC can appear in multiple case types — excluded.
    # "petition_writ_of_mandate" and "petition_habeas_corpus" are civil/criminal
    # and ambiguous enough to exclude.
}


def extract_case_type_from_motion_type(motion_type: str) -> str | None:
    """Infer case type from a normalized motion type string.

    This is a final fallback when case number prefix, LLM extraction, and
    scraper_id all fail to determine the case type.  Only unambiguous motion
    types are mapped; motion types that can appear across multiple case types
    (e.g. ``ex_parte_application``, ``osc``) return ``None``.

    Parameters
    ----------
    motion_type : str
        The normalized motion type value (e.g. ``"motion_to_compel"``).

    Returns
    -------
    str | None
        One of the case type strings, or ``None`` if the motion type is
        ambiguous or not recognized.
    """
    if not motion_type:
        return None
    return _MOTION_TYPE_CASE_TYPE_MAP.get(motion_type.strip())


def extract_case_type_from_number(case_number: str) -> str | None:
    """Infer case type from a California case number prefix.

    California courts embed case type information in case number prefixes
    (e.g. ``CVRI2502741`` is civil, ``FL2301234`` is family).  This provides
    a zero-cost fallback when the LLM does not return a ``case_type``.

    Returns one of the ``CASE_TYPE_VALUES`` strings, or ``None`` if the
    prefix is not recognized.
    """
    if not case_number:
        return None
    case_number = case_number.strip()
    if not case_number:
        return None
    for pattern, case_type in _CASE_TYPE_PREFIX_PATTERNS:
        if pattern.match(case_number):
            return case_type
    return None


# Business / organization keywords used by _looks_like_person_name().
# Compiled once at module load time.  Uses word boundaries so that
# "COUNTY OF" matches at the start of the string.
_ORG_KEYWORD_RE = re.compile(
    r"\b(?:"
    r"LLC|INC|CORP|CORPORATION|LTD|L\.P\.|N\.A\."
    r"|GROUP|MEDICAL|HOSPITAL|CLINIC|INSURANCE|BANK"
    r"|ASSOCIATES|PARTNERS|FOUNDATION|UNIVERSITY|COLLEGE"
    r"|SCHOOL|DISTRICT|AUTHORITY|COMPANY|ENTERPRISES"
    r"|SERVICES|SOLUTIONS|INDUSTRIES|HOLDINGS|PROPERTIES"
    r"|MANAGEMENT|HEALTHCARE|FINANCIAL|INVESTMENTS"
    r"|TRUST|ESTATE|ASSOCIATION|SOCIETY|UNION"
    r"|COUNTY\s+OF|CITY\s+OF|STATE\s+OF|PEOPLE\s+OF|DEPARTMENT\s+OF"
    r"|D/B/A|DBA"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_person_name(name: str) -> bool:
    """Return True if *name* plausibly represents a person (judge) rather than an organization.

    This is a heuristic guard against false positives where party names like
    "Heritage Medical Group" or "Bank of America, N.A." are extracted as judge
    names by the broad regex patterns in ``_JUDGE_NAME_PATTERNS``.

    Checks applied (any failure → False):
    - Rejects names containing common business/organization suffixes.
    - Rejects names containing "vs" or "v." (case title leaked into the field).
    - Rejects names that are too long (> 60 chars) — real judge names are short.
    - Rejects names that are too short (< 3 chars) — need at least first + last.
    """
    if not name:
        return False

    # Too long or too short
    if len(name) > 60 or len(name) < 3:
        return False

    # Normalize for matching
    upper = name.upper()

    # Case title leaked into name: contains "VS" or "V." separator
    if re.search(r"\bVS\b", upper) or re.search(r"\bV\.", upper):
        return False

    if _ORG_KEYWORD_RE.search(upper):
        return False

    # "A CALIFORNIA CORPORATION" or similar entity descriptors
    if re.search(r"\bA\s+CALIFORNIA\b", upper):
        return False

    return True


# ---------------------------------------------------------------------------
# Motion text detection (#1245)
# ---------------------------------------------------------------------------

# Keywords that indicate a line is a motion description rather than a case
# title.  When any of these appear in text that matched the "X v. Y" pattern,
# we reject it as a false positive.  Compiled once at module load time.
_MOTION_KEYWORD_RE = re.compile(
    r"\b(?:"
    r"MOTION|GRANTING|DENYING|ORDER|RULING|RULING\s+ON"
    r"|DISQUALIFY|COMPEL|STRIKE|DISMISS|DEMURRER"
    r"|PETITION|APPLICATION|JUDGMENT|SUMMARY"
    r"|RELIEF|VACATE|QUASH|SANCTIONS|DEFAULT"
    r"|LEAVE|AMEND|RECONSIDERATION|INJUNCTION"
    r"|ARBITRATION|BIFURCATE|CONSOLIDATE|SEVER"
    r"|EX\s+PARTE|PROTECTIVE\s+ORDER"
    r"|WRIT|MANDATE|HABEAS|ATTORNEY.?S?\s+FEES"
    r"|CONTINUANCE|RECLASSIF"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_motion_text(text: str) -> bool:
    """Return True if *text* looks like a motion description rather than a case title.

    This guards against false positives where legal motion text like
    "Order Granting Motion to Disqualify Plaintiff's Designated Expert"
    is split at "v." and matched as a case title (#1245).

    The check looks for motion/legal keywords on either side of the "v."
    separator.  Real case titles have person/entity names on both sides;
    motion descriptions have legal procedure keywords.
    """
    if not text:
        return False

    # Split on "v." / "vs" / "vs." separator to get the two sides
    sides = re.split(r"\s+[Vv][Ss]?\.?\s+", text, maxsplit=1)
    if len(sides) < 2:
        # No "v." separator found — not a case title pattern at all
        return False

    # If EITHER side contains motion keywords, this is likely a motion
    # description, not a case title.
    for side in sides:
        if _MOTION_KEYWORD_RE.search(side):
            return True

    return False


# ---------------------------------------------------------------------------
# Case title extraction — caption block helpers (merged from backfill #1405)
# ---------------------------------------------------------------------------
# These helpers extract titles from structured caption blocks commonly found
# in LA and other county rulings.  They were previously duplicated in
# scripts/backfill_case_titles.py and are now the single source of truth.

# Formal plaintiff/defendant role designations on their own line.
_P_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
_D_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
# Inline format: ", Plaintiff(s), vs."
_P_ROLE_INLINE_RE = re.compile(
    r",\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?\s*,",
)
_D_ROLE_INLINE_RE = re.compile(
    r",\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?[,.]",
)
_VS_RE = re.compile(r"\bv(?:s)?\.", re.IGNORECASE)

# Descriptors that follow a party name and should be stripped.
_DESCRIPTOR_RE = re.compile(
    r",?\s*(?:an individual|a (?:public|private|California|Delaware)"
    r"[\w\s,]*?(?:entity|company|corporation|trust|llc|inc\.?))"
    r"[\s,]*$",
    re.IGNORECASE,
)

# MOVING PARTY / RESPONDING PARTY field patterns.
_MOVING_PARTY_RE = re.compile(
    r"MOVING PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)
_RESPONDING_PARTY_RE = re.compile(
    r"(?:RESPONDING|OPPOSING) PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)
# Role prefixes to strip from moving/responding party names.
_ROLE_PREFIX_RE = re.compile(
    r"^(?:Defendants?|Plaintiffs?|Petitioners?|Respondents?"
    r"|Cross-Complainants?|Cross-Defendants?)[,\s]+",
    re.IGNORECASE,
)

# "Case Name:" / "Case Title:" inline field.
_CASE_NAME_FIELD_RE = re.compile(
    r"CASE\s+(?:NAME|TITLE)\s*:\s*(?P<title>.+?)(?:\s+CASE\s+NUMBER|\s*$)",
    re.IGNORECASE | re.MULTILINE,
)


def _clean_caption_party_name(raw: str) -> str:
    """Normalise a captured party name from a caption block.

    Collapses whitespace, strips trailing commas/et al., removes
    descriptors like "an individual", and title-cases.
    """
    name = " ".join(raw.split()).strip()
    # Strip descriptors like ", an individual" or ", a public entity"
    name = _DESCRIPTOR_RE.sub("", name).strip().rstrip(",").strip()
    # Strip "et al." suffix
    name = re.sub(r",?\s*et\s+al\.?\s*$", "", name, flags=re.IGNORECASE).strip()
    # Remove stray leading/trailing punctuation
    name = name.strip(")(,.; ")
    return name


def _extract_from_caption_block(ruling_text: str) -> str | None:
    """Extract a case title from the formal Plaintiff/Defendant caption block.

    Looks for line-anchored Plaintiff/Defendant keywords (which distinguish
    the caption block from body text), then extracts names from the
    surrounding text.
    """
    # Step 1: find "Plaintiff" as a standalone role designation.
    p_match = _P_ROLE_RE.search(ruling_text)
    if p_match is None:
        p_match = _P_ROLE_INLINE_RE.search(ruling_text)
    if p_match is None:
        return None

    # Step 2: find "vs." or "v." after the plaintiff role.
    vs_match = _VS_RE.search(ruling_text, p_match.end())
    if vs_match is None:
        return None
    if vs_match.start() - p_match.end() > 30:
        return None

    # Step 3: find "Defendant" after vs.
    d_match = _D_ROLE_RE.search(ruling_text, vs_match.end())
    if d_match is None:
        d_match = _D_ROLE_INLINE_RE.search(ruling_text, vs_match.end())
    if d_match is None:
        return None
    if d_match.start() - vs_match.end() > 300:
        return None

    # Step 4: extract plaintiff name — text before the Plaintiff line.
    search_start = max(0, p_match.start() - 300)
    plaintiff_raw = ruling_text[search_start : p_match.start()]
    lines = plaintiff_raw.split("\n")

    name_lines: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped or stripped == ",":
            if name_lines:
                break
            continue
        upper = stripped.upper()
        # Stop at structural header lines
        if (
            upper in ("DISTRICT", "CALIFORNIA", "DEPARTMENT")
            or upper.startswith("SUPERIOR COURT")
            or upper.startswith("FOR THE")
            or upper.startswith("COUNTY OF")
        ):
            break
        # Stop at single-char/number lines (department designators)
        if len(stripped) <= 2 and not stripped.endswith(","):
            break
        if len(name_lines) >= 4:
            break
        name_lines.append(stripped)

    if not name_lines:
        return None

    name_lines.reverse()
    plaintiff = " ".join(name_lines)

    # Step 5: extract defendant name — text between vs. and Defendant line
    defendant_raw = ruling_text[vs_match.end() : d_match.start()]

    plaintiff = _clean_caption_party_name(plaintiff)
    defendant = _clean_caption_party_name(defendant_raw)

    if not plaintiff or not defendant:
        return None

    title = f"{plaintiff.title()} v. {defendant.title()}"

    if len(title) > 150:
        return None

    return title


def _extract_from_moving_responding(ruling_text: str) -> str | None:
    """Extract a case title from MOVING PARTY / RESPONDING PARTY fields.

    Many LA rulings list parties as::

        MOVING PARTY: Defendant Acme Corp.
        RESPONDING PARTY: Plaintiffs John Doe and Jane Doe

    Strips the role prefix (Defendant/Plaintiffs/etc.) and constructs
    "[Moving Party] v. [Responding Party]".
    """
    m_match = _MOVING_PARTY_RE.search(ruling_text)
    if m_match is None:
        return None
    r_match = _RESPONDING_PARTY_RE.search(ruling_text)
    if r_match is None:
        return None

    moving_raw = m_match.group("name").strip()
    responding_raw = r_match.group("name").strip()

    # Reject non-party content like "No opposition filed"
    skip_phrases = ("no opposition", "none", "no response", "unopposed")
    for phrase in skip_phrases:
        if phrase in responding_raw.lower():
            return None

    # Strip role prefixes like "Defendant " or "Plaintiffs "
    moving_name = _ROLE_PREFIX_RE.sub("", moving_raw)
    responding_name = _ROLE_PREFIX_RE.sub("", responding_raw)

    moving_name = _clean_caption_party_name(moving_name)
    responding_name = _clean_caption_party_name(responding_name)

    if not moving_name or not responding_name:
        return None

    title = f"{moving_name.title()} v. {responding_name.title()}"

    if len(title) > 150:
        return None

    return title


def _extract_from_case_name_field(ruling_text: str) -> str | None:
    """Extract a case title from an inline 'Case Name:' or 'Case Title:' field.

    Some LA rulings include a metadata field like::

        CASE NAME: Porsche Leasing Ltd. et al. v. Tsisana Mikia, et al.
    """
    m = _CASE_NAME_FIELD_RE.search(ruling_text)
    if m is None:
        return None

    raw_title = m.group("title").strip()

    # Must contain "v." to be a real case name (not just a description)
    if not re.search(r"\bv\.?\s", raw_title):
        return None

    title = " ".join(raw_title.split())
    title = title.rstrip(".,;: ")

    if len(title) > 150 or len(title) < 5:
        return None

    return title


# ---------------------------------------------------------------------------
# Case title extraction — regex patterns
# ---------------------------------------------------------------------------

# Patterns to extract case titles ("Plaintiff v. Defendant") from ruling text.
# Ordered by specificity — most anchored patterns first.
_CASE_TITLE_PATTERNS: list[re.Pattern[str]] = [
    # NOTE: "Case Name: X v. Y" was previously here but is now handled by
    # Strategy 2 (_extract_from_case_name_field) which includes a "v." check
    # to avoid returning non-adversarial content like "Motion for Summary
    # Judgment" as a case title.  See #1405.
    #
    # "PLAINTIFF vs DEFENDANT" with case number prefix, Riverside-style
    # e.g. "CVPS2306157 YELDELL vs HENSS Hearing re: Demurrer"
    re.compile(
        r"(?:^|\n)\s*(?:\S+\s+)"
        r"(?P<title>[A-Z][A-Za-z,.'\- ]+?\s+vs?\.?\s+[A-Z][A-Za-z,.'\- ]+?)"
        r"\s+(?:Hearing|Motion|Demurrer|Petition|Application|Order)",
        re.MULTILINE,
    ),
    # "Name v. Name et al., No. CaseNumber" — LA inline header (#337)
    # e.g. "Raymond Yawen Wu v. Steve Tsui et al., No. 25STCV34748"
    re.compile(
        r"(?:^|\n)\s*(?P<title>"
        r"[A-Z][A-Za-z,.'\- ]{1,60}"
        r"\s+[Vv][Ss]?\.?\s+"
        r"[A-Z][A-Za-z,.'\- ]{1,60}"
        r")"
        r"(?:,?\s*No\.\s*\S+)?",  # optional trailing ", No. XXXXX"
        re.MULTILINE,
    ),
    # Multi-line: "Name v. Name" on one line, case number on the next (#337)
    # e.g. "Raymond Yawen Wu v. Steve Tsui et al.\n  No. 25STCV34748"
    re.compile(
        r"(?:^|\n)\s*(?P<title>"
        r"[A-Z][A-Za-z,.'\- ]{1,60}"
        r"\s+[Vv][Ss]?\.?\s+"
        r"[A-Z][A-Za-z,.'\- ]{1,60}"
        r")\s*\n\s*(?:No\.\s*\S+)?",
        re.MULTILINE,
    ),
    # Generic "X v. Y" or "X vs Y" or "X vs. Y" — broad fallback
    re.compile(
        r"(?:^|\n)\s*(?P<title>"
        r"[A-Z][A-Za-z,.'\- ]{1,60}"
        r"\s+[Vv][Ss]?\.?\s+"
        r"[A-Z][A-Za-z,.'\- ]{1,60}"
        r")",
        re.MULTILINE,
    ),
]

# Trailing noise to strip from extracted titles — case number references,
# "et al." punctuation artifacts, etc.
_TITLE_TRAILING_NOISE_RE = re.compile(
    r",?\s*No\.\s*\S+\s*$"  # ", No. 25STCV34748"
    r"|,?\s*Case\s+No\.\s*\S+\s*$",  # ", Case No. 25STCV34748"
    re.IGNORECASE,
)


_DEPT_HEADER_BOILERPLATE_TITLE_RE = re.compile(
    r"DEPARTMENT\s+\S+\s+LAW AND MOTION RULINGS",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# "In re" / "In the Matter of" / "Petition of" patterns (#1378)
# ---------------------------------------------------------------------------
# Non-adversarial cases (probate, guardianship, family law) that lack
# "Plaintiff v. Defendant" structure.  Used as a fallback after all
# "v." patterns have been tried.
_IN_RE_PATTERNS: list[re.Pattern[str]] = [
    # "In re: Name" or "In re Name" (with or without colon)
    re.compile(
        r"(?:^|\n)\s*(?P<title>(?:In\s+re:?\s+|In\s+the\s+Matter\s+of\s+)"
        r"[A-Z][^\n]{2,})",
        re.IGNORECASE | re.MULTILINE,
    ),
    # "Petition of Name ..."
    re.compile(
        r"(?:^|\n)\s*(?P<title>Petition\s+of\s+[A-Z][^\n]{2,})",
        re.IGNORECASE | re.MULTILINE,
    ),
]


def extract_case_title(ruling_text: str) -> str | None:
    """Extract a case title from ruling text.

    Tries multiple extraction strategies in order of reliability (#1405):

    1. Formal caption block (Plaintiff vs. Defendant) — most reliable
    2. Inline "Case Name:" or "Case Title:" field — direct extraction
    3. "MOVING PARTY:" / "RESPONDING PARTY:" fields — construct from party names
    4. Regex "X v. Y" patterns — broad fallback with boilerplate/motion filtering
    5. "In re:" / "In the Matter of" / "Petition of" — non-adversarial cases (#1378)

    Returns a title like ``"Buenaventura v. City Of Pasadena"``, or ``None``.

    Rejects titles that contain department header boilerplate (#1244).
    Rejects matches that look like motion descriptions rather than case
    titles (#1245) — e.g. "Granting Motion To v. Disqualify Plaintiff".
    """
    # Strategy 1: Formal caption block (Plaintiff vs. Defendant)
    title = _extract_from_caption_block(ruling_text)
    if title is not None:
        return title

    # Strategy 2: Inline "Case Name:" / "Case Title:" field
    title = _extract_from_case_name_field(ruling_text)
    if title is not None:
        return title

    # Strategy 3: MOVING PARTY / RESPONDING PARTY fields
    title = _extract_from_moving_responding(ruling_text)
    if title is not None:
        return title

    # Strategy 4: Regex "X v. Y" patterns — broad fallback
    for pattern in _CASE_TITLE_PATTERNS:
        # Use finditer to check all matches for this pattern, not just the
        # first.  If the first match is a motion description, we skip it and
        # try subsequent matches rather than abandoning the pattern (#1245).
        for m in pattern.finditer(ruling_text):
            title = m.group("title").strip()
            # Strip trailing case number references: ", No. 25STCV34748"
            title = _TITLE_TRAILING_NOISE_RE.sub("", title).strip()
            # Reject titles containing department header boilerplate (#1244)
            if _DEPT_HEADER_BOILERPLATE_TITLE_RE.search(title):
                continue
            # Reject motion descriptions masquerading as case titles (#1245).
            if _looks_like_motion_text(title):
                continue
            # Detect all-caps before normalizing the "v." separator.
            # Strip the separator and check if the remaining name parts
            # are all uppercase (the separator itself may be lowercase "vs").
            _name_parts = re.split(r"\s+[Vv][Ss]?\.?\s+", title)
            is_all_caps = all(p == p.upper() and p.strip() for p in _name_parts)
            # Normalize: "AASI vs HONDA" → "Aasi v. Honda"
            title = re.sub(r"\s+[Vv][Ss]?\.?\s+", " v. ", title)
            # Title-case if all-caps — apply to name parts only, not separator
            if is_all_caps:
                parts = title.split(" v. ")
                title = " v. ".join(p.title() for p in parts)
            # Clean trailing punctuation
            title = title.rstrip(".,;: ")
            if len(title) >= 5:
                return title

    # Strategy 5: "In re" / "In the Matter of" / "Petition of" (#1378)
    for pattern in _IN_RE_PATTERNS:
        m = pattern.search(ruling_text)
        if m is not None:
            title = m.group("title").strip()
            # Collapse whitespace
            title = " ".join(title.split())
            # Strip trailing punctuation
            title = title.rstrip(".,;: ")
            if 5 <= len(title) <= 150:
                return title

    return None


# ---------------------------------------------------------------------------
# Hearing date extraction
# ---------------------------------------------------------------------------

# Common date formats found in California tentative rulings.
# Ordered by specificity — "Month DD, YYYY" first (most common in court PDFs),
# then numeric formats.
_HEARING_DATE_PATTERNS: list[tuple[re.Pattern[str], list[str]]] = [
    # "February 24, 2026" or "February 24 2026" (with or without comma)
    (
        re.compile(
            r"(?:January|February|March|April|May|June|July|August"
            r"|September|October|November|December)"
            r"\s+\d{1,2},?\s+\d{4}",
            re.IGNORECASE,
        ),
        ["%B %d, %Y", "%B %d %Y"],
    ),
    # "Date: 03/04/26" or "Date: 03/04/2026"
    (
        re.compile(
            r"Date:\s*(\d{1,2}/\d{1,2}/\d{2,4})",
            re.IGNORECASE,
        ),
        ["%m/%d/%Y", "%m/%d/%y"],
    ),
    # Standalone MM/DD/YYYY or MM/DD/YY (less anchored — try last)
    (
        re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b"),
        ["%m/%d/%Y"],
    ),
]


def extract_hearing_date(ruling_text: str) -> date | None:
    """Extract a hearing date from ruling text using common date patterns.

    Returns the first successfully parsed date, or ``None`` if no pattern
    matches.  This is a fallback for when scrapers do not populate
    ``hearing_date`` in the event payload.
    """
    for pattern, formats in _HEARING_DATE_PATTERNS:
        m = pattern.search(ruling_text)
        if not m:
            continue
        # Use capture group if present, otherwise full match
        raw = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
        raw = " ".join(raw.split())  # normalize whitespace
        for fmt in formats:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Party extraction from case captions
# ---------------------------------------------------------------------------

# Corporate suffixes that must not be split from the preceding name when
# the caption is comma-separated.  Matches with or without trailing period.
_CORPORATE_SUFFIX_RE = re.compile(
    r"^(?:Inc|LLC|Corp|Corporation|Ltd|L\.?P\.?|N\.?A\.?"
    r"|Co|PA|PC|PLLC|LLP|DBA|GP)\.?$",
    re.IGNORECASE,
)

# Pattern to extract "Plaintiff(s) v. Defendant(s)" from a case title string.
_CAPTION_VS_RE = re.compile(
    r"^(?P<plaintiff_side>.+?)"
    r"\s+v[Ss]?\.?\s+"
    r"(?P<defendant_side>.+)$",
)

# Suffixes to strip from each side (et al., etc.)
_ET_AL_RE = re.compile(r",?\s*et\s+al\.?\s*$", re.IGNORECASE)


def _split_caption_names(text: str) -> list[str]:
    """Split a caption side (e.g. "Caldera, et al.") into individual names.

    Handles comma-separated lists while keeping corporate suffixes attached
    to the preceding entity name (e.g. "Techno-Advanced, Inc." stays intact).
    """
    # Strip "et al." before splitting
    text = _ET_AL_RE.sub("", text).strip()
    if not text:
        return []

    # Split on ", and " (Oxford comma) or ", "
    raw_parts = re.split(r",\s+and\s+|,\s+", text)
    if len(raw_parts) == 1:
        raw_parts = re.split(r"\s+and\s+", text)

    # Reassemble corporate suffixes onto the preceding name
    parts: list[str] = []
    for fragment in raw_parts:
        fragment = fragment.strip().strip(")(,.; ")
        if not fragment:
            continue
        if parts and _CORPORATE_SUFFIX_RE.match(fragment):
            parts[-1] = f"{parts[-1]}, {fragment}"
        else:
            parts.append(fragment)

    return [p for p in parts if len(p) >= 2]


def _is_name_fragment(name: str) -> bool:
    """Return True if *name* looks like a fragment rather than a complete name.

    Fragments are single tokens shorter than 4 characters that are not
    recognizable abbreviations (like "Jr" or initials).
    """
    stripped = name.strip().rstrip(".")
    # Single very short token with no spaces — likely a fragment
    if " " not in stripped and len(stripped) < 3:
        return True
    # Corporate suffix on its own is a fragment
    if _CORPORATE_SUFFIX_RE.match(stripped):
        return True
    return False


def extract_parties_from_caption(case_title: str) -> list[dict[str, str]]:
    """Extract party names and roles from a case title / caption string.

    Parses "Plaintiff v. Defendant" style captions and returns a list of
    party dicts with ``name`` and ``role`` keys.  This is a fallback used
    by the ingestion pipeline when the scraper does not provide structured
    party data.

    Corporate suffixes (Inc, LLC, Corp, etc.) are kept with their parent
    entity name.  Name fragments are filtered out.

    Returns an empty list if the caption cannot be parsed.
    """
    if not case_title:
        return []

    m = _CAPTION_VS_RE.match(case_title.strip())
    if m is None:
        return []

    parties: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for side, role in [
        (m.group("plaintiff_side"), "plaintiff"),
        (m.group("defendant_side"), "defendant"),
    ]:
        names = _split_caption_names(side)
        for name in names:
            # Title-case normalization
            name = name.strip().rstrip(".,;: ")
            if not name or _is_name_fragment(name):
                continue
            key = (name.lower(), role)
            if key in seen:
                continue
            seen.add(key)
            parties.append({"name": name, "role": role})

    return parties


def extract_judge_name(ruling_text: str) -> str | None:
    """Extract a judge name from ruling text using court-specific regex patterns.

    Tries multiple patterns used by California court scrapers (LA, SB, SF,
    Riverside, OC).  Returns the first matched name stripped of whitespace,
    or ``None`` if no pattern matches.

    After a pattern match, the extracted name is validated with
    ``_looks_like_person_name`` to reject false positives where party or
    organization names are captured instead of judge names (see #326).

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
            if name and _looks_like_person_name(name):
                return name
    return None
