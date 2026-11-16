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
# Usage (live PR — reads the current title and body over REST, #5133):
#   scripts/check-pr-title.sh --pr <N> [--repo <owner/name>]
#   `scripts/gh-pr-with-retry.sh merge` runs this immediately before merging,
#   so the text being merged is what gets checked.
#
# Exit codes:
#   0 — Title and body look good.
#   1 — Placeholder title or empty body detected.
#   2 — Usage error, or the PR could not be read (--pr mode).

set -euo pipefail

TITLE="${PR_TITLE:-}"
BODY="${PR_BODY:-}"

PR_NUMBER=""
REPO="judgemind/judgemind"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --pr)
            PR_NUMBER="${2:-}"
            shift 2 || { echo "ERROR: --pr requires a value." >&2; exit 2; }
            ;;
        --repo)
            REPO="${2:-}"
            shift 2 || { echo "ERROR: --repo requires a value." >&2; exit 2; }
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [[ -n "$PR_NUMBER" ]]; then
    if ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
        echo "ERROR: --pr must be a positive integer, got '$PR_NUMBER'." >&2
        exit 2
    fi
    # REST (core quota), so this still works when GraphQL is exhausted.
    if ! TITLE=$(gh api "/repos/$REPO/pulls/$PR_NUMBER" --jq '.title // ""'); then
        echo "ERROR: could not read the title of PR #$PR_NUMBER." >&2
        exit 2
    fi
    if ! BODY=$(gh api "/repos/$REPO/pulls/$PR_NUMBER" --jq '.body // ""'); then
        echo "ERROR: could not read the body of PR #$PR_NUMBER." >&2
        exit 2
    fi
fi

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
