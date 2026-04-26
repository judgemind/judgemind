"""Helper for collapsing long comment threads before passing to the summary phase.

Public API
----------
estimate_tokens(text)        -- rough character-based token estimate.
collapse_comments(comments)  -- identity when under threshold; structural
                                collapse when over.

This module is intentionally kept deterministic and pure-Python: no LLM calls,
no I/O, no side effects.  The /task-v2-plan skill may optionally enrich
collapsed entries with one-line LLM summaries inline, but the helper itself
stays testable without any model dependency.
"""

from __future__ import annotations

# venv: none (stdlib only)
# one-off: false


def estimate_tokens(text: str) -> int:
    """Return a rough token estimate for *text*.

    Uses the heuristic ``int(len(text) / 3.5)`` — fast, deterministic, and
    consistent with the 4-chars-per-token rule of thumb for English prose.
    """
    return int(len(text) / 3.5)


def collapse_comments(
    comments: list[dict],
    threshold_tokens: int = 2000,
) -> list[dict]:
    """Return *comments* collapsed when the total body exceeds *threshold_tokens*.

    Rules:
    - Empty list → empty list (identity).
    - ≤3 entries → always verbatim, regardless of size (not enough entries to
      safely keep first + last 2 without an empty or degenerate middle).
    - Total estimated tokens < threshold → verbatim (same list object, same
      dicts — callers may check identity).
    - Total estimated tokens >= threshold → structural collapse:
        [comments[0], *collapsed_middle, comments[-2], comments[-1]]
      where each middle entry is a new dict with:
        ``author``    — original author
        ``date``      — original date
        ``body``      — ``"(collapsed) " + body[:120].replace("\\n", " ")``
        ``_collapsed`` — ``True``

    Args:
        comments:         List of comment dicts.  Each dict is expected to
                          carry at least a ``body`` key; ``author`` and
                          ``date`` are preserved when present.
        threshold_tokens: Token count above which collapse is triggered.
                          Defaults to 2000.

    Returns:
        Original list (identity) or a new list of collapsed dicts.
    """
    if not comments:
        return comments

    # ≤3 entries: always verbatim — not enough to collapse meaningfully.
    if len(comments) <= 3:
        return comments

    total_tokens = sum(estimate_tokens(c.get("body", "")) for c in comments)

    if total_tokens < threshold_tokens:
        return comments

    # Structural collapse: keep first, collapse middle, keep last two.
    first = comments[0]
    last_two = comments[-2:]
    middle_originals = comments[1:-2]

    collapsed_middle = []
    for entry in middle_originals:
        body = entry.get("body", "")
        collapsed_body = "(collapsed) " + body[:120].replace("\n", " ")
        collapsed_entry: dict = {"body": collapsed_body, "_collapsed": True}
        if "author" in entry:
            collapsed_entry["author"] = entry["author"]
        if "date" in entry:
            collapsed_entry["date"] = entry["date"]
        collapsed_middle.append(collapsed_entry)

    return [first, *collapsed_middle, *last_two]
