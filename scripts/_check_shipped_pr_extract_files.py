#!/usr/bin/env python3
# _check_shipped_pr_extract_files.py — Extract candidate file paths from an
# issue body for scripts/check-shipped-pr.sh.
#
# Reads JSON {"body": "...", "title": "..."} on stdin, prints unique candidate
# file paths (one per line) to stdout. Pure stdlib — no external imports —
# so it runs from any worktree without venv setup.
#
# venv: none
# permanent: true
#
# The regex matches the four conventional repo roots
#   scripts/, packages/, docs/, infra/
# plus `.github/`. It stops at whitespace, `]`, `)`, backticks, or quotes —
# the boundaries that terminate inline file references in markdown bodies.
# A trailing punctuation strip removes `.`, `,`, `:`, `;` (sentence
# punctuation that often follows an inline file reference).

import json
import re
import sys

# Match (?:scripts/|packages/|docs/|infra/|\.github/) followed by any
# non-terminator characters. Note: the spec calls for these five roots; we
# intentionally omit `.claude/` and other roots here because issue bodies
# rarely cite them as the locus of changes — the five chosen roots cover
# the vast majority of "this lands at <path>" references.
PATH_REGEX = re.compile(
    r"(?:scripts/|packages/|docs/|infra/|\.github/)[^\s\]\)`\"',]+"
)

# Strip these trailing characters from a hit (sentence punctuation that
# often follows an inline file reference but is not part of the path).
TRAILING_STRIP = ".,:;"


def extract_files(body: str) -> list[str]:
    """Return unique candidate file paths from `body` in first-seen order."""
    if not body:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in PATH_REGEX.finditer(body):
        path = match.group(0)
        # Strip trailing punctuation
        while path and path[-1] in TRAILING_STRIP:
            path = path[:-1]
        # Skip glob-y entries (`scripts/tests/*.sh`) — the commits API
        # rejects globs and they would just produce 404s.
        if "*" in path or "?" in path:
            continue
        # Skip suspiciously short hits (<6 chars after the root prefix)
        # to suppress false positives like `docs/`. The shortest legit
        # repo paths run ~10+ chars (e.g. "docs/A.md", "scripts/x.sh").
        if len(path) < 8:
            continue
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 1
    body = data.get("body") or ""
    title = data.get("title") or ""
    # Combine title + body for extraction (title rarely cites paths but
    # cheap to include).
    combined = f"{title}\n{body}"
    for path in extract_files(combined):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
