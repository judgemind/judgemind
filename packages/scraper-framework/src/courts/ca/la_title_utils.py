"""Shared title extraction helpers for LA County backfill scripts.

These functions extract case titles from ruling text using two strategies:
1. Formal plaintiff/defendant caption blocks (e.g. "Smith, Plaintiff(s), vs. Jones, Defendant(s).")
2. MOVING PARTY / RESPONDING PARTY fields

Both strategies clean party names by stripping entity descriptors,
role prefixes, and trailing punctuation before assembling "Plaintiff v. Defendant".

The ``max_length`` parameter defaults to ``_MAX_TITLE_LENGTH`` (120) for the
live scraper, but backfill callers can pass a larger limit (e.g. 250) to accept
multi-party titles that are legitimately longer after entity descriptor stripping.
"""

from __future__ import annotations

import re

from courts.ca.la_tentatives import (
    _DEPT_HEADER_BOILERPLATE_RE,
    _ENTITY_DESCRIPTOR_RE,
    _MAX_TITLE_LENGTH,
    _MOVING_PARTY_RE,
    _RESPONDING_PARTY_RE,
    _ROLE_PREFIX_RE,
)

# ---------------------------------------------------------------------------
# Caption block extraction patterns (not in la_tentatives.py, which uses a
# different _CASE_TITLE_RE regex for structured anchor-based extraction).
# These patterns work against plain-text ruling content.
# ---------------------------------------------------------------------------

P_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
D_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
VS_RE = re.compile(r"\bv(?:s)?\.", re.IGNORECASE)


def clean_name(raw: str) -> str:
    """Clean a party name: strip entity descriptors, whitespace, punctuation.

    This is the backfill-oriented variant that strips entity descriptors
    inline (unlike ``la_tentatives._clean_party_name`` which defers
    descriptor stripping to ``_sanitize_title``).
    """
    name = " ".join(raw.split()).strip()
    name = _ENTITY_DESCRIPTOR_RE.sub("", name).strip()
    name = re.sub(r"[;,]\s*$", "", name).strip()
    name = re.sub(r"\s+And\s*$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r",?\s*Et\.?\s*Al\.?\s*$", ", et al.", name, flags=re.IGNORECASE).strip()
    name = name.strip(")(,.; ")
    return name


def extract_title_from_caption(
    ruling_text: str,
    *,
    max_length: int = _MAX_TITLE_LENGTH,
) -> str | None:
    """Extract title from formal plaintiff/defendant caption block.

    Args:
        ruling_text: The raw ruling text to search.
        max_length: Maximum allowed title length.  Defaults to
            ``_MAX_TITLE_LENGTH`` (120).
    """
    p_match = P_ROLE_RE.search(ruling_text)
    d_match = D_ROLE_RE.search(ruling_text)
    vs_match = VS_RE.search(ruling_text)

    if not (p_match and d_match and vs_match):
        return None

    # Plaintiff name: text before the plaintiff role marker
    p_start = max(0, p_match.start() - 500)
    p_text = ruling_text[p_start : p_match.start()]
    lines = [ln.strip() for ln in p_text.split("\n") if ln.strip()]
    if not lines:
        return None
    plaintiff_raw = lines[-1].rstrip(",")

    # Defendant name: text between vs. and defendant role marker
    vs_end = vs_match.end()
    d_start = d_match.start()
    if vs_end >= d_start:
        return None
    defendant_raw = ruling_text[vs_end:d_start].strip()
    d_lines = [ln.strip() for ln in defendant_raw.split("\n") if ln.strip()]
    if not d_lines:
        return None
    defendant_raw = " ".join(d_lines).rstrip(",")

    plaintiff = clean_name(plaintiff_raw)
    defendant = clean_name(defendant_raw)

    if not plaintiff or not defendant:
        return None

    title = f"{plaintiff.title()} v. {defendant.title()}"
    if len(title) > max_length or len(title) < 5:
        return None
    return title


def extract_title_from_moving_responding(
    ruling_text: str,
    *,
    max_length: int = _MAX_TITLE_LENGTH,
) -> str | None:
    """Extract title from MOVING PARTY / RESPONDING PARTY fields.

    Args:
        ruling_text: The raw ruling text to search.
        max_length: Maximum allowed title length.  Defaults to
            ``_MAX_TITLE_LENGTH`` (120).
    """
    m_match = _MOVING_PARTY_RE.search(ruling_text)
    if m_match is None:
        return None
    r_match = _RESPONDING_PARTY_RE.search(ruling_text)
    if r_match is None:
        return None

    moving_raw = m_match.group("name").strip()
    responding_raw = r_match.group("name").strip()

    skip_phrases = ("no opposition", "none", "no response", "unopposed")
    for phrase in skip_phrases:
        if phrase in responding_raw.lower():
            return None

    moving = clean_name(_ROLE_PREFIX_RE.sub("", moving_raw))
    responding = clean_name(_ROLE_PREFIX_RE.sub("", responding_raw))

    if not moving or not responding:
        return None

    title = f"{moving.title()} v. {responding.title()}"
    if len(title) > max_length or len(title) < 5:
        return None
    return title


def extract_clean_title(
    ruling_text: str,
    *,
    max_length: int = _MAX_TITLE_LENGTH,
) -> str | None:
    """Try multiple strategies to extract a clean case title from ruling text.

    Args:
        ruling_text: The raw ruling text to search.
        max_length: Maximum allowed title length.  Defaults to
            ``_MAX_TITLE_LENGTH`` (120).
    """
    title = extract_title_from_caption(ruling_text, max_length=max_length)
    if title and not _DEPT_HEADER_BOILERPLATE_RE.search(title):
        return title

    title = extract_title_from_moving_responding(ruling_text, max_length=max_length)
    if title and not _DEPT_HEADER_BOILERPLATE_RE.search(title):
        return title

    return None
