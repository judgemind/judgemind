#!/usr/bin/env bash
# Check whether a GitHub issue was filed by a trusted author.
#
# Usage: scripts/check-issue-author.sh <issue-number>
#
# Exit codes:
#   0 — trusted author (OWNER, MEMBER, COLLABORATOR)
#   1 — untrusted author
#   2 — error (e.g. API failure)
#
# Trusted means the issue author has write access or higher on the repo.
# The dispatcher and /task skill call this before picking up work.

set -euo pipefail

ISSUE="${1:?Usage: check-issue-author.sh <issue-number>}"
REPO="judgemind/judgemind"
TRUSTED_ASSOCIATIONS="OWNER MEMBER COLLABORATOR"

# Fetch the issue's author_association via the REST API.
# gh issue view doesn't expose author_association, so use the API directly.
ASSOC=$(gh api "repos/${REPO}/issues/${ISSUE}" --jq '.author_association' 2>/dev/null) || {
    echo "ERROR: Failed to fetch issue #${ISSUE}" >&2
    exit 2
}

if echo "$TRUSTED_ASSOCIATIONS" | grep -qw "$ASSOC"; then
    echo "TRUSTED: Issue #${ISSUE} author association is ${ASSOC}"
    exit 0
else
    echo "UNTRUSTED: Issue #${ISSUE} author association is ${ASSOC}"
    exit 1
fi
