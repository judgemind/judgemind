#!/usr/bin/env python3
# _check_issue_companion_closed_inspect.py — Detect "companion-closed"
# obsoletion signal in an issue body for scripts/check-issue-companion-closed.sh.
#
# venv: none
# permanent: true
#
# Helper for scripts/check-issue-companion-closed.sh — see issue #4557.
#
# Reads the issue body on stdin and a JSON map of `{cited_number: {state,
# stateReason}}` from $SIBLING_STATES_JSON. Emits one of:
#
#   companion-closed:<N>     — at least one cited #N is `closed` AND
#                              `stateReason == COMPLETED` AND is framed
#                              by an adjacent companion keyword
#                              ("when #N", "after #N", "until
#                              #N", "once #N", "removed when #N",
#                              "blocked on #N", "blocked by #N",
#                              "depends on #N"). The first such match
#                              wins.
#   clear:no-references      — body cites no `#N` at all
#   clear:no-companion       — cites #N references, but none are inside a
#                              companion-framed clause
#   clear:no-closed-completed — at least one companion-framed cite, but
#                              no cited sibling is `closed` AND
#                              `stateReason == COMPLETED` (e.g. all
#                              still-open, or `state_reason == NOT_PLANNED`)
#
# Output: one line to stdout, exit 0 on success. Exit 1 on malformed
# input.
#
# Why keyword-adjacent scoping (not whole-body or whole-paragraph): bare
# hashtags like "see #4408 for context" or "Closes #4408" are not
# companion framing; they're informational links. Incident narratives
# ("This happened during #4661 verification. Right after the deploy,
# ...") put a framing word in the same paragraph without it framing the
# cite — paragraph scoping false-fired on exactly that (#4685). So a
# keyword only frames a `#N` when it precedes the cite in the same
# clause with at most MAX_GAP_WORDS words between them ("removed when
# the structural fix in #4408 lands"). See `_companion_framed_cites`.

from __future__ import annotations

import json
import os
import re
import sys


# Match every `#NNN` token in a string. Captures the bare digits.
HASHTAG_REGEX = re.compile(r"#(\d+)\b")

# Companion-framing keywords. Each is matched with `\b` boundaries to
# avoid substring false-fires ("Once" matches the start of a sentence
# as well as "atOnce" wouldn't — `\b` keeps the latter from matching).
# Multi-word phrases are matched as adjacent-word sequences with
# tolerant whitespace (`\s+`).
#
# The list is the union of:
#   - the AC's required keywords: when, after, until, once, removed
#     when, blocked on, blocked by, depends on
#   - the issue body's framing variants observed in the canonical
#     #4409 ↔ #4408 example: "should be removed when the structural
#     fix in #N lands"
COMPANION_KEYWORD_PATTERNS = [
    r"\bremoved\s+when\b",
    r"\bblocked\s+on\b",
    r"\bblocked\s+by\b",
    r"\bdepends\s+on\b",
    r"\bwhen\b",
    r"\bafter\b",
    r"\buntil\b",
    r"\bonce\b",
]
COMPANION_KEYWORD_REGEX = re.compile(
    "|".join(COMPANION_KEYWORD_PATTERNS),
    re.IGNORECASE,
)


def _split_paragraphs(body: str) -> list[str]:
    """Split body into paragraph chunks separated by blank lines.

    Markdown headings (lines starting with ``#``) are kept inside the
    paragraph they precede — we never need to read them separately.
    """
    if not body:
        return []
    # Normalize line endings.
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    # Split on one-or-more blank lines.
    chunks = re.split(r"\n\s*\n", normalized)
    # Drop empty / whitespace-only chunks.
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _cites_in_chunk(chunk: str) -> list[str]:
    """Return the list of `#N` references inside the chunk, in order."""
    return [m.group(1) for m in HASHTAG_REGEX.finditer(chunk)]


# Adjacency rule (#4685): a keyword frames a `#N` only when it comes
# BEFORE the cite, in the same clause, with at most MAX_GAP_WORDS words
# between them. "removed when the structural fix in #4408 lands" has 4
# words in the gap ("the structural fix in"); the #4665 incident
# narrative ("during #4661 verification ... Right after the deploy")
# puts the keyword after the cite, in another sentence, so it doesn't
# count. When unsure, report "not framed" — a false negative only costs
# a normal /task run, a false positive sends /task down verify-and-close.
MAX_GAP_WORDS = 5

# Anything in the keyword→cite gap that ends the clause: sentence/clause
# punctuation, parentheses/brackets, or a line break that starts a new
# markdown list item. A bare newline (hard-wrapped prose) is allowed.
_GAP_BREAK_REGEX = re.compile(r"[,;.!?()\[\]]|\n\s*(?:[-*+]|\d+[.)])\s")


def _companion_framed_cites(chunk: str) -> list[str]:
    """Return the `#N` cites in the chunk that a companion keyword frames.

    Cites are returned in document order, without duplicates.
    """
    keyword_ends = [m.end() for m in COMPANION_KEYWORD_REGEX.finditer(chunk)]
    out: list[str] = []
    for cite in HASHTAG_REGEX.finditer(chunk):
        num = cite.group(1)
        if num in out:
            continue
        for kw_end in keyword_ends:
            if kw_end > cite.start():
                break
            gap = chunk[kw_end : cite.start()]
            if _GAP_BREAK_REGEX.search(gap):
                continue
            if len(gap.split()) <= MAX_GAP_WORDS:
                out.append(num)
                break
    return out


def _has_companion_framing(chunk: str) -> bool:
    """Return True if any `#N` in the chunk is framed by an adjacent keyword."""
    return bool(_companion_framed_cites(chunk))


def _is_closed_completed(state_info: object) -> bool:
    """Return True if the sibling is closed and state_reason is COMPLETED.

    Defensive against missing keys / unexpected casing — the GitHub
    enum is uppercase (``COMPLETED`` / ``NOT_PLANNED`` / ``REOPENED``)
    but we lower-case for comparison so a future API change to
    title-case doesn't silently break the check.

    Returns False for:
    - state == "open" (regardless of stateReason)
    - state == "closed" AND stateReason == "not_planned"
    - state == "closed" AND stateReason missing or null (treated as
      ambiguous — do not fire)
    """
    if not isinstance(state_info, dict):
        return False
    state = str(state_info.get("state") or "").strip().lower()
    state_reason = str(state_info.get("stateReason") or "").strip().lower()
    return state == "closed" and state_reason == "completed"


def main() -> int:
    raw_states = os.environ.get("SIBLING_STATES_JSON", "{}") or "{}"
    try:
        sibling_states = json.loads(raw_states)
    except json.JSONDecodeError:
        return 1
    if not isinstance(sibling_states, dict):
        return 1

    body = sys.stdin.read()
    paragraphs = _split_paragraphs(body)

    all_cites: set[str] = set()
    # Use a list to preserve document order — the first companion-framed
    # cite that resolves closed-completed is the surfaced match.
    companion_framed_cites_ordered: list[str] = []
    seen_companion: set[str] = set()

    for chunk in paragraphs:
        cites = _cites_in_chunk(chunk)
        if not cites:
            continue
        all_cites.update(cites)
        for cite in _companion_framed_cites(chunk):
            if cite not in seen_companion:
                seen_companion.add(cite)
                companion_framed_cites_ordered.append(cite)

    if not all_cites:
        print("clear:no-references")
        return 0
    if not companion_framed_cites_ordered:
        print("clear:no-companion")
        return 0

    # Walk in document order; surface the FIRST cite that resolves
    # closed-completed. Document order matches author intent — the
    # canonical "removed when #N lands" sentence usually appears
    # before any later narrative cites of unrelated companions.
    for cite in companion_framed_cites_ordered:
        if _is_closed_completed(sibling_states.get(cite)):
            print(f"companion-closed:{cite}")
            return 0

    print("clear:no-closed-completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
