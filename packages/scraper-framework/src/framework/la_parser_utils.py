"""Shared LA County parser regex patterns and utility functions.

Centralizes regex patterns and helper functions used by the LA tentative
rulings scraper (``courts.ca.la_tentatives``) and multiple backfill scripts
(``backfill_case_titles``, ``backfill_la_entity_descriptors``,
``backfill_la_header_titles``, ``backfill_parties``,
``backfill_role_label_titles``).

Prior to this module, these patterns were duplicated in 6 files — any bug
fix (e.g. #1425) had to be applied to all copies.  By centralizing them
here in the installed ``scraper-framework`` package, ECS oneshot scripts
can import them directly without violating the single-file constraint.

See #1465 for the deduplication rationale.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Role prefix stripping
# ---------------------------------------------------------------------------

# Role prefixes to strip from party names.  Matches role labels followed by
# comma and/or whitespace (e.g. "Defendant ", "Plaintiffs, ").
ROLE_PREFIX_RE = re.compile(
    r"^(?:Defendants?|Plaintiffs?|Petitioners?|Respondents?"
    r"|Cross-Complainants?|Cross-Defendants?)[,\s]+",
    re.IGNORECASE,
)

# Role labels that should never appear as standalone party names.
BARE_ROLE_LABELS: frozenset[str] = frozenset(
    w.lower()
    for w in (
        "Defendant",
        "Defendants",
        "Plaintiff",
        "Plaintiffs",
        "Petitioner",
        "Petitioners",
        "Respondent",
        "Respondents",
        "Cross-Complainant",
        "Cross-Complainants",
        "Cross-Defendant",
        "Cross-Defendants",
    )
)

# ---------------------------------------------------------------------------
# Moving / Responding party field patterns
# ---------------------------------------------------------------------------

# Pattern: "MOVING PARTY: [name]" or "MOVING PARTIES: [name]"
MOVING_PARTY_RE = re.compile(
    r"MOVING PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)

# Pattern: "RESPONDING PARTY: [name]" or "OPPOSING PARTY: [name]"
RESPONDING_PARTY_RE = re.compile(
    r"(?:RESPONDING|OPPOSING) PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Caption block patterns
# ---------------------------------------------------------------------------

# Formal caption block regex — captures plaintiff/defendant names and roles.
# Matches: "NAME,\n  Plaintiff(s),\n  vs.\n  NAME,\n  Defendant(s)."
CASE_PARTIES_RE = re.compile(
    r"^(?P<plaintiff>.+?),?\s*\n\s*"
    r"(?P<p_role>Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?,?"
    r"\s+vs\.\s+"
    r"(?P<defendant>.+?),?\s*\n\s*"
    r"(?P<d_role>Defendant|Respondent|Cross-Defendant)\(?s?\)?\.?",
    re.DOTALL | re.MULTILINE,
)

# Map caption role keywords to normalized role values for the case_parties table.
ROLE_MAP: dict[str, str] = {
    "plaintiff": "plaintiff",
    "petitioner": "petitioner",
    "cross-complainant": "cross_complainant",
    "defendant": "defendant",
    "respondent": "respondent",
    "cross-defendant": "cross_defendant",
}

# Plaintiff/Defendant role markers in caption blocks.
# These match standalone role designations at line start (not mid-sentence).
P_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
D_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)

# Also match inline format: ", Plaintiff(s), vs."
P_ROLE_INLINE_RE = re.compile(
    r",\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?\s*,",
)
D_ROLE_INLINE_RE = re.compile(
    r",\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?[,.]",
)

# "vs." or "v." separator
VS_RE = re.compile(r"\bv(?:s)?\.", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Case name / title field pattern
# ---------------------------------------------------------------------------

# Inline "Case Name: [text]" or "Case Title: [text]" field
CASE_NAME_FIELD_RE = re.compile(
    r"CASE\s+(?:NAME|TITLE)\s*:\s*(?P<title>.+?)(?:\s+CASE\s+NUMBER|\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Entity descriptor pattern
# ---------------------------------------------------------------------------

# Entity descriptor phrases that should be stripped from party names in titles.
# These inflate short-form "Smith v. Jones" into long captions.  Order matters:
# longer/more-specific phrases first so they match before their substrings.
ENTITY_DESCRIPTOR_RE = re.compile(
    r",?\s*(?:"
    r"An Individual(?:\s+And Derivatively On Behalf Of [^,;]+)?"
    r"|An? (?:Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut"
    r"|Delaware|District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois"
    r"|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts"
    r"|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada"
    r"|New Hampshire|New Jersey|New Mexico|New York|North Carolina"
    r"|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island"
    r"|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia"
    r"|Washington|West Virginia|Wisconsin|Wyoming)"
    r" (?:Corporation|Limited Liability Company|Limited Partnership"
    r"|General Partnership|Business Entity|Nonprofit Corporation|Public Entity)"
    r"|A (?:Corporation|Limited Liability Company|Limited Partnership"
    r"|General Partnership|Business Entity|Nonprofit Corporation|Public Entity)"
    r"|Individually And As [^,;]+"
    r"|By And Through [^,;]+"
    r"|As Trustee Of [^,;]+"
    # Hyphenated and non-hyphenated variants of successor/administrator phrases
    r"|Successor[- ]In[- ]Interest To(?:\s+And [^,;]+)?"
    r"|Administrator Of [^,;]+"
    r"|As Administrator [^,;]+"
    r"|Derivatively On Behalf Of [^,;]+"
    r"|As An Individual"
    r"|Form Unknown"
    r"|Doe(?:s)? \d+ (?:To|Through) \d+(?:,? Inclusive)?"
    r")",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum length for a cleaned party name.  btree index limit is ~900 chars.
MAX_PARTY_NAME_LENGTH = 500

# Phrases in responding-party text that indicate no real opposing party.
SKIP_RESPONDING_PHRASES: tuple[str, ...] = (
    "no opposition",
    "none",
    "no response",
    "unopposed",
)
