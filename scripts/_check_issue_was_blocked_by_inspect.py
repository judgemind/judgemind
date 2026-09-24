#!/usr/bin/env python3
# _check_issue_was_blocked_by_inspect.py — Detect the "was-blocked-by,
# now-closed" zombie-blocker signal in an issue body for
# scripts/check-issue-was-blocked-by.sh.
#
# venv: none
# permanent: true
#
# Helper for scripts/check-issue-was-blocked-by.sh — see issue #4610.
#
# Reads the issue body on stdin and a JSON map of `{cited_number: {state,
# stateReason}}` from $SIBLING_STATES_JSON. The body's durable provenance
# marker (left by scripts/_unblock_dependents.py / unblock-issues.yml when
# they auto-restore agent/ready) has the shape::
#
#     Was-blocked-by: #4282, #4297, #4370 (all closed-completed 2026-05-08; auto-unblocked)
#
# Emits one of:
#
#   was-blocked-by:<comma-joined-nums>
#                            — a Was-blocked-by marker exists AND EVERY cited
#                              former blocker resolves to `state == closed`
#                              AND `stateReason == COMPLETED`. The caller
#                              pivots to verify-and-close.
#   clear:no-marker          — body has no `Was-blocked-by:` marker (or the
#                              marker cites no `#N`)
#   clear:not-all-closed-completed
#                            — a marker exists, but at least one cited former
#                              blocker is still open, closed-as-not_planned,
#                              missing, or otherwise unresolved
#
# Output: one line to stdout, exit 0 on success. Exit 1 only on malformed
# $SIBLING_STATES_JSON.

from __future__ import annotations

import json
import os
import re
import sys


# Match the provenance marker line and capture its tail (the `#N` cites live
# in the captured group). Anchored at line start (after optional whitespace).
MARKER_LINE_REGEX = re.compile(r"^\s*Was-blocked-by:\s*(.*)$", re.MULTILINE)

# Match every `#NNN` token in a string. Captures the bare digits.
HASHTAG_REGEX = re.compile(r"#(\d+)\b")


def _extract_marker_cites(body: str) -> list[str]:
    """Return the `#N` former-blocker numbers cited in the marker, in order.

    Scans every ``Was-blocked-by:`` line and collects the cited numbers,
    de-duplicating while preserving first-seen order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for marker_tail in MARKER_LINE_REGEX.findall(body):
        for m in HASHTAG_REGEX.finditer(marker_tail):
            n = m.group(1)
            if n and n not in seen:
                seen.add(n)
                out.append(n)
    return out


def _is_closed_completed(state_info: object) -> bool:
    """Return True if the former blocker is closed and stateReason COMPLETED.

    Defensive against missing keys / unexpected casing — the GitHub enum is
    uppercase (``COMPLETED`` / ``NOT_PLANNED`` / ``REOPENED``) but we
    lower-case for comparison.

    Returns False for:
    - state == "open" (regardless of stateReason)
    - state == "closed" AND stateReason == "not_planned"
    - state == "closed" AND stateReason missing or null (ambiguous)
    - missing / non-dict entry
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
    cites = _extract_marker_cites(body)

    if not cites:
        print("clear:no-marker")
        return 0

    if all(_is_closed_completed(sibling_states.get(cite)) for cite in cites):
        print(f"was-blocked-by:{','.join(cites)}")
        return 0

    print("clear:not-all-closed-completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
