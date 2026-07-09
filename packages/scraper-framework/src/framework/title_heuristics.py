"""Shared, dependency-light heuristics for uninformative party titles.

Single source of truth for detecting *role-literal* party titles
(``Plaintiffs v. Defendant``) and *bracketed-placeholder* titles
(``... v. [Defendant not specified]``).  These primitives were extracted
verbatim out of :mod:`framework.llm_extractor` so both the extractor-stage
orphan-drop filter and the leaf :mod:`validation.deterministic` DB-write gate
can share them.

This module intentionally imports **only** the standard-library :mod:`re` so
the leaf ``validation`` layer can depend on it without pulling in the heavy
``anthropic`` import chain that :mod:`framework.llm_extractor` carries.

See #4618 (role-literal on either side of ``v.``), #3988/#4002 (bracketed
placeholder envelope), and #4628 (uninformative-title orphan-drop gate).
"""

from __future__ import annotations

import re

# A single role token, allowing singular/plural and the cross-prefixed forms.
# Used by :func:`is_role_literal_title` to test whether a ``/``-separated piece
# of a title segment is a pure role word.  See #4618.
ROLE_TOKEN = r"(?:Cross-)?(?:Plaintiff|Defendant|Petitioner|Respondent|Complainant)s?"

# Matches a title segment (one side of ``v.``) that is ENTIRELY a role-literal
# compound — every ``/``-separated piece is a role token, with no trailing real
# text.  Anchored start-to-end so ``Defendants Inc.`` (real company) does NOT
# match while ``Defendants/Cross-Complainants`` does.  See #4618.
ROLE_LITERAL_SEGMENT_RE = re.compile(
    rf"^\s*{ROLE_TOKEN}(?:\s*/\s*{ROLE_TOKEN})*\s*$",
    re.IGNORECASE,
)

# Splits a title on the ``v.``/``vs.``/``v``/``vs`` party separator using a word
# boundary so it does not fire inside a real word.  See #4618.
VS_SEPARATOR_RE = re.compile(r"\bv[s]?\.?\s+", re.IGNORECASE)


def is_role_literal_title(title: str | None) -> bool:
    """Return True when EITHER side of the ``v.`` separator is a role-literal (#4618).

    A *role-literal title* is one where the LLM substituted a role word
    ("Plaintiff", "Defendant", "Petitioner", "Respondent", their plurals, and the
    cross-prefixed / ``/``-joined compounds) for a real party name, on the left,
    the right, or both sides of the ``v.``/``vs.`` separator. Examples that match:

    - ``Plaintiff v. General Motors, LLC`` (left)
    - ``Acme Corp v. Defendants`` (right)
    - ``Tcfi Cp Llc v. Defendants/Cross-Complainants`` (right compound)
    - ``Plaintiff/Petitioner v. Defendant/Respondent`` (both)

    The detection is anchored to the WHOLE segment, so a real party name that
    merely contains a role word as a substring is NOT flagged:

    - ``Smith v. Defendants Inc.`` (trailing real text → not role-literal)
    - ``Plaintiff Holdings LLC v. Smith`` (leading role word + real text)
    - ``Aasi v. American Honda`` / ``Doe v. Roe`` (clean)

    Returns ``False`` for ``None``, empty, whitespace-only, or any title without a
    recognisable ``v.`` separator.
    """
    if not title or not title.strip():
        return False
    parts = VS_SEPARATOR_RE.split(title, maxsplit=1)
    if len(parts) != 2:
        return False
    left, right = parts
    return bool(ROLE_LITERAL_SEGMENT_RE.match(left) or ROLE_LITERAL_SEGMENT_RE.match(right))


# Matches a case_title that contains an LLM-hallucinated bracketed placeholder
# for a party name, e.g. "Ezra Arce v. [Defendant not specified]" or
# "[Plaintiff name unknown] v. Smith".  The bracket must contain a role word
# (plaintiff/defendant/petitioner/respondent/party/name/case) followed by a
# qualifier (not specified, unknown, missing, not listed, not provided, tbd).
# No anchor — the bracket can appear anywhere in the title.  See #3988.
#
# Explicit non-match envelope (do NOT widen without product confirmation — #4002):
#   - Role-only:        [DEFENDANT], [Defendant 1]  — no qualifier word present
#   - Qualifier-only:   [TBD], [Insert defendant here]  — no leading role word
#   - Possessives:      [defendant's name]  — possessive breaks the role-word token
#   - Ordinals:         [Defendant 1]  — digit suffix is not a qualifier keyword
#   - Free-form:        [Name to be determined]  — "Name" alone is not a role word
#                       used in this position; qualifier phrase not in the allowlist
BRACKETED_PLACEHOLDER_TITLE_RE = re.compile(
    r"\[(?:plaintiff|defendant|petitioner|respondent|party|name|case)[^\]]*"
    r"(?:not specified|unknown|missing|not listed|not provided|tbd)[^\]]*\]",
    re.IGNORECASE,
)


def has_uninformative_party_title(title: str | None) -> bool:
    """True when *title* is empty/whitespace, a role-literal, or a bracketed placeholder.

    A title that is a role literal (``Plaintiffs v. Defendant``) or a bracketed
    LLM placeholder (``... v. [Defendant not specified]``) carries no real party
    information — for the purpose of the ``UNKNOWN-`` orphan-drop gate it is
    equivalent to a null/empty title.
    """
    if title is None or not title.strip():
        return True
    return is_role_literal_title(title) or bool(BRACKETED_PLACEHOLDER_TITLE_RE.search(title))
