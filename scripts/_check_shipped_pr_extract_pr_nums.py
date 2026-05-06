#!/usr/bin/env python3
# _check_shipped_pr_extract_pr_nums.py — Extract every `(#N)` token from
# commit headlines for scripts/check-shipped-pr.sh.
#
# Reads commit messages on stdin (one per line — typically the output of
# `gh api /repos/.../commits?path=<file> --jq '.[] | .commit.message'`).
# For each line, takes the headline (everything before the first newline,
# but the input is already line-split so each line IS a headline) and
# extracts EVERY `(#N)` token. Prints the resulting PR numbers, one per
# line, deduplicated in first-seen order.
#
# venv: none
# permanent: true
#
# Why this exists: the bash regex `[[ "$str" =~ \(#([0-9]+)\) ]]` only
# captures the FIRST match in the string. On commit headlines like:
#
#     fix(ci): vercel-deploy-status no longer false-fails on squash-merge (#2837) (#3170)
#
# the bash version would extract only `2837` (the closed-by issue
# referenced in the conventional-commits subject) and drop `3170` (the
# actual squash-merge PR). The downstream `gh pr view 2837 --json files`
# then errors out and the candidate is silently skipped — even though
# PR #3170 absolutely shipped the change. Bash doesn't do iterative
# regex matches natively, so we delegate to Python and let the
# downstream vetting loop in check-shipped-pr.sh sort out which `(#N)`
# is a real merged PR (issue numbers and unknown PRs return null
# `mergedAt` and are dropped by _check_shipped_pr_overlap.py).
#
# See issue #4214 for the bug + fix design, and PR #3170's headline as
# the canonical multi-`(#N)` example.

import re
import sys

# Match every `(#NNN)` token in a string. Returns the captured number(s).
PR_NUM_REGEX = re.compile(r"\(#(\d+)\)")


def extract_pr_nums(commit_messages: list[str]) -> list[str]:
    """Return unique PR numbers from `(#N)` tokens, in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for message in commit_messages:
        if not message:
            continue
        # Each input line is already a headline — but defensively grab
        # only the first \n-delimited segment in case a multi-line commit
        # message slipped through.
        headline = message.split("\n", 1)[0]
        for match in PR_NUM_REGEX.finditer(headline):
            pr_num = match.group(1)
            if pr_num not in seen:
                seen.add(pr_num)
                out.append(pr_num)
    return out


def main() -> int:
    lines = sys.stdin.read().splitlines()
    for pr_num in extract_pr_nums(lines):
        print(pr_num)
    return 0


if __name__ == "__main__":
    sys.exit(main())
