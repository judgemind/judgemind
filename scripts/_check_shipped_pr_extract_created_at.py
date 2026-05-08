#!/usr/bin/env python3
# _check_shipped_pr_extract_created_at.py — Extract the `createdAt` field
# from an issue JSON for scripts/check-shipped-pr.sh.
#
# Reads `gh issue view --json body,title,createdAt` JSON on stdin
# (other fields are ignored). Prints the `createdAt` ISO-8601 timestamp
# (e.g. `2026-05-08T19:41:29Z`) to stdout, or nothing if the field is
# absent / null / malformed.
#
# venv: none
# permanent: true
#
# Exit codes:
#   0 — Always. The helper degrades gracefully: a missing or malformed
#       `createdAt` is treated as "no date guard available" by the
#       caller, which is fail-open (preserve pre-#4353 behavior). No
#       exit-1 path because every recoverable input shape ends in
#       "print empty string + return 0".
#
# Why this lives in its own helper rather than being merged into
# _check_shipped_pr_extract_files.py: separation of concerns. The
# extract_files helper emits N path lines on stdout (one per candidate
# file); adding a sentinel-prefixed `createdAt:` line to the same stream
# would force every consumer to learn the new format. A standalone
# helper that emits a single bare timestamp is simpler to consume from
# bash and can be unit-tested in isolation.

import json
import sys


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Malformed JSON — fail open. The caller will skip the date
        # guard entirely, preserving pre-#4353 behavior.
        return 0

    created_at = data.get("createdAt")
    if isinstance(created_at, str) and created_at:
        # GitHub returns ISO-8601 UTC with a trailing `Z`
        # (e.g. `2026-05-08T19:41:29Z`). Lexicographic string comparison
        # is correct for this format — the caller does not need to
        # parse it into a datetime.
        print(created_at)
    # Otherwise: print nothing. The caller checks for empty string and
    # skips the date guard.
    return 0


if __name__ == "__main__":
    sys.exit(main())
