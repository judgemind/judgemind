#!/usr/bin/env bash
# check-pr-title.sh — Reject placeholder PR titles and empty PR bodies.
#
# permanent: true
#
# Flags PRs where the title matches a known placeholder pattern or the body is
# empty/whitespace-only.  These are the two most common causes of "WIP:" or
# "ralph output" titles landing in the merge queue (#3994).
#
# Patterns rejected (case-insensitive, anchored at start of title):
#   - "WIP:"
#   - "ralph output"
#   - "placeholder"
#
# Usage (CI — env-var form):
#   PR_TITLE="..." PR_BODY="..." scripts/check-pr-title.sh
#
# Exit codes:
#   0 — Title and body look good.
#   1 — Placeholder title or empty body detected.

set -euo pipefail

TITLE="${PR_TITLE:-}"
BODY="${PR_BODY:-}"

# ─── Check title ───────────────────────────────────────────────────────
if printf '%s' "$TITLE" | grep -iEq '^(WIP:|ralph output|placeholder)'; then
    echo "ERROR: PR title looks like a placeholder: \"$TITLE\""
    echo ""
    echo "  Placeholder titles (WIP:, ralph output, placeholder) are not allowed."
    echo "  Remediation: task-v2-summary did not run — re-trigger or amend the"
    echo "  PR title/body manually before merging."
    echo ""
    exit 1
fi

# ─── Check body ────────────────────────────────────────────────────────
# Strip all whitespace and newlines; if nothing remains, the body is empty.
stripped_body=""
if [[ -n "$BODY" ]]; then
    stripped_body="$(printf '%s' "$BODY" | tr -d '[:space:]')"
fi

if [[ -z "$stripped_body" ]]; then
    echo "ERROR: PR body is empty or whitespace-only."
    echo ""
    echo "  A non-empty PR body is required."
    echo "  Remediation: task-v2-summary did not run — re-trigger or amend the"
    echo "  PR title/body manually before merging."
    echo ""
    exit 1
fi

echo "PR title and body look good."
exit 0
