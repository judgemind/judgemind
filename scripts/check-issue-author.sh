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

# Bot accounts whose issues are trusted. github-actions[bot] creates issues
# from CI workflows (data-quality-check, api-error-check, etc.) that need
# autonomous handling. Only repo collaborators can define these workflows,
# so the trust is transitive.
TRUSTED_BOTS="github-actions[bot]"

# Fetch the issue's author_association and login via the REST API (single call).
# gh issue view doesn't expose author_association, so use the API directly.
# Output format: "ASSOCIATION login_name" (space-separated).
RESULT=$(gh api "repos/${REPO}/issues/${ISSUE}" --jq '"\(.author_association) \(.user.login)"') || {
    echo "ERROR: Failed to fetch issue #${ISSUE}" >&2
    exit 2
}

ASSOC="${RESULT%% *}"
LOGIN="${RESULT#* }"

# Guard against empty/null responses — fail closed.
if [[ -z "$ASSOC" || "$ASSOC" == "null" ]]; then
    echo "ERROR: author_association is empty or null for issue #${ISSUE}" >&2
    exit 2
fi

# Check trusted bot accounts first.
if [[ "$LOGIN" == *"[bot]" ]] && echo "$TRUSTED_BOTS" | grep -qwF "$LOGIN"; then
    echo "TRUSTED: Issue #${ISSUE} author '${LOGIN}' is a trusted bot"
    exit 0
fi

if echo "$TRUSTED_ASSOCIATIONS" | grep -qw "$ASSOC"; then
    echo "TRUSTED: Issue #${ISSUE} author '${LOGIN}' association is ${ASSOC}"
    exit 0
else
    echo "UNTRUSTED: Issue #${ISSUE} author '${LOGIN}' association is ${ASSOC}"
    exit 1
fi
