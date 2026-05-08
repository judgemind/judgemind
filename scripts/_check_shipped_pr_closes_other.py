#!/usr/bin/env python3
# _check_shipped_pr_closes_other.py — Filter out PR candidates whose body
# closes a different issue, for scripts/check-shipped-pr.sh.
#
# Reads `gh pr view --json body` JSON on stdin (other fields are ignored).
# Reads the current issue number from CHECK_SHIPPED_ISSUE_NUM env var.
# Exits 0 ("filter this candidate out") if the PR body contains a
# closing-keyword reference (`Closes #N` / `Fixes #N` / `Resolves #N`,
# case-insensitive, all 9 GitHub verb forms) that names AT LEAST ONE issue
# whose number is NOT the current issue. Exits 1 ("keep this candidate")
# otherwise — including the canonical case of an empty body or a body that
# only contains free prose with no closing keywords (which is exactly the
# placeholder-titled WIP PR shape that motivates the script).
#
# venv: none
# permanent: true
#
# Why "names a different issue" rather than "any closing keyword present":
# A PR whose body says `Closes #N` for the *same* issue is the canonical
# happy-path PR and should be retained as a candidate (any false positive
# in that case would be the duplicate-PR check's job to flag, not ours).
# A PR whose body contains BOTH `Closes #<this-issue>` AND
# `Closes #<other-issue>` should also be retained — it explicitly closes
# the issue we're checking. Only a PR whose only closing-keyword
# references point at OTHER issues is filtered out.
#
# Closing keywords recognized (https://docs.github.com/en/issues/tracking-
# your-work-with-issues/linking-a-pull-request-to-an-issue):
#   close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved.
# Each followed by `#N`, `owner/repo#N`, or
# `https://github.com/owner/repo/issues/N`.

import json
import os
import re
import sys

# All 9 GitHub closing-keyword verbs, case-insensitive.
_CLOSE_VERBS = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)

# Regex captures the issue/PR number after a closing keyword in any of
# the three GitHub-recognized forms:
#   Closes #N
#   Closes owner/repo#N
#   Closes https://github.com/owner/repo/issues/N
# `(?i)` for case-insensitive verbs. `\b` ensures we don't match e.g.
# "preclose" or "fixed-up". Uses non-capturing groups for the verb and
# the optional owner/repo prefix so the only capture group is the issue
# number itself.
_CLOSE_VERB_ALT = "|".join(_CLOSE_VERBS)
_CLOSE_KEYWORD_RE = re.compile(
    r"(?i)\b(?:" + _CLOSE_VERB_ALT + r")\s*:?\s*"
    r"(?:[\w.\-]+/[\w.\-]+)?"  # optional owner/repo prefix
    r"#(\d+)"
)
_CLOSE_URL_RE = re.compile(
    r"(?i)\b(?:" + _CLOSE_VERB_ALT + r")\s*:?\s*"
    r"https?://github\.com/[\w.\-]+/[\w.\-]+/issues/(\d+)"
)


def extract_closed_issue_numbers(body: str) -> set[int]:
    """Return the set of issue numbers a PR body's closing keywords reference."""
    if not body:
        return set()
    nums: set[int] = set()
    for m in _CLOSE_KEYWORD_RE.finditer(body):
        try:
            nums.add(int(m.group(1)))
        except (TypeError, ValueError):
            continue
    for m in _CLOSE_URL_RE.finditer(body):
        try:
            nums.add(int(m.group(1)))
        except (TypeError, ValueError):
            continue
    return nums


def main() -> int:
    issue_num_str = os.environ.get("CHECK_SHIPPED_ISSUE_NUM", "")
    try:
        current_issue = int(issue_num_str)
    except (TypeError, ValueError):
        # Without a current issue number we cannot decide; fail open
        # (exit 1 = keep the candidate). Caller will still apply the
        # file-overlap threshold.
        return 1

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Malformed PR JSON — fail open.
        return 1

    body = data.get("body") or ""
    closed = extract_closed_issue_numbers(body)
    if not closed:
        # No closing-keyword references — canonical placeholder-PR
        # shape. Keep candidate.
        return 1
    if current_issue in closed:
        # PR explicitly closes the issue we're checking (with or
        # without also closing other issues). Keep candidate — this is
        # the legitimate happy-path PR, and the duplicate-PR check is
        # the right gate for that case.
        return 1
    # Every closing-keyword reference in the PR body points at an issue
    # OTHER than the one we're checking. Filter this candidate out.
    return 0


if __name__ == "__main__":
    sys.exit(main())
