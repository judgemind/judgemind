#!/usr/bin/env python3
# _check_shipped_pr_summary.py — Emit JSON summary for a shipped match
# detected by scripts/check-shipped-pr.sh.
#
# venv: none
# permanent: true
#
# Reads metadata from environment variables (so the parent shell does not
# have to construct JSON via heredocs or printf escapes):
#   CHECK_SHIPPED_ISSUE          — issue number (string)
#   CHECK_SHIPPED_PR             — PR number (string)
#   CHECK_SHIPPED_OVERLAP_COUNT  — number of overlapping files (string int)
#   CHECK_SHIPPED_OVERLAP_FILES  — comma-separated overlap file paths
#   CHECK_SHIPPED_CANDIDATE_FILES — comma-separated all-candidate paths
#
# Prints a pretty JSON object to stdout. The shape is stable so callers
# (e.g. /task SKILL.md's pivot path) can `jq` against it.

import json
import os
import sys


def main() -> int:
    issue = os.environ.get("CHECK_SHIPPED_ISSUE", "")
    pr = os.environ.get("CHECK_SHIPPED_PR", "")
    overlap_count_str = os.environ.get("CHECK_SHIPPED_OVERLAP_COUNT", "0")
    overlap_files_csv = os.environ.get("CHECK_SHIPPED_OVERLAP_FILES", "")
    added_files_csv = os.environ.get("CHECK_SHIPPED_ADDED_FILES", "")
    candidate_files_csv = os.environ.get("CHECK_SHIPPED_CANDIDATE_FILES", "")

    try:
        overlap_count = int(overlap_count_str)
    except ValueError:
        overlap_count = 0

    overlap_files = [f for f in overlap_files_csv.split(",") if f]
    added_files = [f for f in added_files_csv.split(",") if f]
    candidate_files = [f for f in candidate_files_csv.split(",") if f]

    summary = {
        "issue": int(issue) if issue.isdigit() else issue,
        "shipped_pr": int(pr) if pr.isdigit() else pr,
        "overlap_count": overlap_count,
        "overlap_files": overlap_files,
        "added_files": added_files,
        "candidate_files": candidate_files,
    }

    # Verify-clause channel (#4472). When the wrapper resolved the match
    # via _check_shipped_pr_verify_probe.py rather than path-overlap, the
    # canonical Verify clause that fired is passed through this env var
    # so the JSON summary can name it. Empty string → no verify-channel
    # match → field is omitted (preserves pre-#4472 JSON shape for
    # path-overlap-driven matches that downstream consumers may already
    # parse).
    verify_clause = os.environ.get("CHECK_SHIPPED_VERIFY_CLAUSE", "")
    if verify_clause:
        summary["verify_clause"] = verify_clause

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
