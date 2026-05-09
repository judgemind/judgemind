#!/usr/bin/env python3
# _check_issue_plan_blocked_inspect.py — Inspect issue comments JSON for
# a recent dispatcher-plan-blocked recommendation marker.
#
# venv: none
# permanent: true
#
# Helper for scripts/check-issue-plan-blocked.sh — see issue #4438.
#
# Reads `gh issue view --json comments` JSON on stdin (other fields
# ignored). Reads three configuration knobs from the environment:
#
#   BOT_LOGINS     — comma- or whitespace-separated list of bot login
#                    names to filter out before picking the "latest
#                    non-bot comment". Defaults to the empty string
#                    (filter no one) so the helper degrades gracefully
#                    if the caller forgets to set the env var.
#   SENTINEL       — the line-1 sentinel HTML comment (default
#                    ``<!-- dispatcher-plan-blocked -->``).
#   FOOTER_PREFIX  — the prefix of the recommendation footer (default
#                    ``<!-- dispatcher-plan-blocked-recommendation:``).
#
# Algorithm:
#   1. Parse the comments array from stdin JSON.
#   2. Filter out comments whose author.login is in BOT_LOGINS.
#   3. Sort by createdAt descending — newest first.
#   4. Take the first remaining comment. If none, emit ``clear:no-comments``.
#   5. If the first comment's body contains SENTINEL, extract the
#      recommendation token from the footer (if present) and emit
#      ``plan-blocked:<token>``. Otherwise emit ``clear:no-marker`` if
#      ANY comment carries the sentinel (a later human comment
#      superseded it: emit ``clear:superseded``), or ``clear:no-marker``
#      if none does.
#
# Output:
#   - On success: writes one line to stdout, no trailing newline beyond
#     ``print()``'s default. Exit 0.
#   - On any malformed input or unexpected error: exits non-zero.
#     Stderr is suppressed by the bash caller; the exit code is the
#     signal.

from __future__ import annotations

import json
import os
import sys


def _bot_login_set() -> set[str]:
    """Parse BOT_LOGINS env var into a set of login strings.

    Accepts comma-separated or whitespace-separated entries; an empty
    or unset value yields the empty set (no filtering).
    """
    raw = os.environ.get("BOT_LOGINS", "") or ""
    # Replace commas with spaces, then split on any whitespace.
    tokens = raw.replace(",", " ").split()
    return {t for t in tokens if t}


def _extract_recommendation(body: str, footer_prefix: str) -> str:
    """Extract the recommendation token from the footer line.

    The footer has shape ``<!-- dispatcher-plan-blocked-recommendation: TOKEN -->``.
    Returns the TOKEN trimmed of whitespace, or the empty string if no
    footer is found in the body. The lookup is line-oriented for
    determinism; we don't try to match a footer split across lines.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith(footer_prefix):
            continue
        # Strip the prefix and the trailing ``-->`` if present.
        tail = stripped[len(footer_prefix) :]
        if tail.endswith("-->"):
            tail = tail[: -len("-->")]
        return tail.strip()
    return ""


def main() -> int:
    sentinel = os.environ.get("SENTINEL", "<!-- dispatcher-plan-blocked -->")
    footer_prefix = os.environ.get(
        "FOOTER_PREFIX",
        "<!-- dispatcher-plan-blocked-recommendation:",
    )
    bots = _bot_login_set()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return 1

    comments = payload.get("comments") or []
    if not isinstance(comments, list):
        return 1

    # Normalize each comment into ``(created_at, author_login, body)``.
    normalized: list[tuple[str, str, str]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        created_at = str(comment.get("createdAt") or "")
        body = comment.get("body") or ""
        if not isinstance(body, str):
            body = str(body)
        author_obj = comment.get("author") or {}
        if isinstance(author_obj, dict):
            author_login = str(author_obj.get("login") or "")
        else:
            author_login = ""
        normalized.append((created_at, author_login, body))

    # Filter out bot comments. ``gh issue view`` always emits ISO-8601
    # createdAt strings, so lexicographic sort matches chronological
    # sort. Defensive: if a comment has an empty createdAt, sort it
    # last (treat as oldest) — the "latest non-bot" pick still works
    # because everything else has a real timestamp.
    non_bot = [c for c in normalized if c[1] not in bots]
    non_bot.sort(key=lambda c: c[0], reverse=True)

    # Determine whether ANY comment (bot-filtered or not) carries the
    # sentinel — used to disambiguate the "no marker" vs "superseded"
    # cases below. Filtering on non_bot only is correct: a bot comment
    # carrying the sentinel doesn't exist today (the daemon posts as
    # the operator's gh CLI auth, not via github-actions[bot]), so
    # restricting to non_bot doesn't miss anything in practice.
    any_sentinel_present = any(sentinel in body for _, _, body in non_bot)

    if not non_bot:
        # No human/operator comments at all — no signal to act on.
        print("clear:no-comments")
        return 0

    latest_body = non_bot[0][2]
    if sentinel in latest_body:
        recommendation = _extract_recommendation(latest_body, footer_prefix)
        # Empty recommendation is fine — older plan-blocked comments
        # (pre-#4438) lack the footer entirely. Emit the bare marker
        # so callers can still pivot, treating the recommendation as
        # the implicit "operator-triage".
        print(f"plan-blocked:{recommendation}")
        return 0

    if any_sentinel_present:
        # A plan-blocked comment exists, but a later human comment
        # superseded it. Treat as clear so /task proceeds normally —
        # the human has acted on the recommendation.
        print("clear:superseded")
        return 0

    print("clear:no-marker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
