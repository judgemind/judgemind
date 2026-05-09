#!/usr/bin/env python3
# venv: none
# permanent: true
"""_gh_comment_with_retry_match.py — Helper for gh-comment-with-retry.sh.

Reads a GitHub issue-comments JSON list from stdin, finds the most recent
comment by ``$GH_USER``, writes its body to ``$LATEST_BODY_FILE``, and
prints the comment's ``html_url`` on stdout.

Why a sibling helper instead of an inline ``python3 -c``
--------------------------------------------------------
Mirrors the ``scripts/_check_shipped_pr_*.py`` pattern: small, focused
helpers next to the bash wrapper that uses them. Keeps the logic
unit-testable on its own (no shell mocking required), avoids the
operator-laptop preflight hook's friction with inline ``python -c``
content (the hook fires on the agent's Bash tool calls; sibling .py
files are fine), and stays under the project's "Multi-line Python
always goes in a .py file" rule for consistency.

Stdin: the raw JSON body of GET /repos/<owner>/<repo>/issues/<N>/comments
(an array of comment objects).

Env:
    GH_USER (str): the gh user login whose comment we want to match.
    LATEST_BODY_FILE (str): destination path for the matched comment's
        body content (UTF-8, written verbatim from the JSON ``body``
        field).

Output:
    On match: writes the body to ``LATEST_BODY_FILE``, prints the
    matched comment's ``html_url`` (or empty string if missing) on
    stdout, exits 0.

    On no-match (no comment by GH_USER in the response): exits 1 with
    no stdout output. The wrapper interprets exit 1 as
    "no-recent-comment-by-us-found".

Tracking: issue #4478.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    """Process the comments JSON from stdin, write+print on match."""
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        # The wrapper saw something that wasn't valid JSON — treat as
        # no-match so the wrapper falls through to its real-failure
        # passthrough. Don't echo the input on stderr; the wrapper
        # already has the original 504 stderr to surface.
        return 1

    if not isinstance(data, list):
        return 1

    gh_user = os.environ.get("GH_USER", "")
    body_path = os.environ.get("LATEST_BODY_FILE", "")
    if not gh_user or not body_path:
        return 1

    # The GitHub API returns comments in chronological order by default,
    # but the wrapper requested ``direction=desc`` so the most recent
    # comment is first. We pick the first entry whose user.login matches
    # GH_USER. If the API ignored ``direction=desc`` for any reason
    # (rare), we'd still pick "first by user," which is a defensible
    # fallback — the wrapper's match check is symmetric (sha256 of body
    # vs body file), so a wrong-comment match is impossible.
    for comment in data:
        if not isinstance(comment, dict):
            continue
        user_obj = comment.get("user") or {}
        login = user_obj.get("login") or "" if isinstance(user_obj, dict) else ""
        if login != gh_user:
            continue
        body = comment.get("body") or ""
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write(body)
        print(comment.get("html_url") or "")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
