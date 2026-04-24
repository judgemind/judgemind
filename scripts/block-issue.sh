#!/usr/bin/env bash
# block-issue.sh — Atomically mark an issue as blocked by another issue.
#
# Adds the `status/blocked` label, removes `agent/ready`, and ensures the
# issue body contains a `## Dependencies` section with a `Blocked by #N` line.
# If the section or line already exists, it is not duplicated.
#
# Usage:
#   scripts/block-issue.sh [--verbose|-v] <issue> <blocker>
#   scripts/block-issue.sh 1144 1143
#
# Options:
#   --verbose, -v   Show detailed progress output (default: terse)
#
# Environment:
#   JM_VERBOSE=1    Same as --verbose
#
# This script exists because agents were inconsistent about how they marked
# issues as blocked — sometimes adding only the label or only a comment, which
# broke the unblock-issues CI workflow. This script ensures both pieces are
# always present.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_terse_lib.sh
source "$SCRIPT_DIR/_terse_lib.sh"
eval "$(terse_parse_verbose_flag "$@")"

if [ $# -ne 2 ]; then
    echo "Usage: scripts/block-issue.sh <issue> <blocker>" >&2
    echo "  <issue>   — the issue number to mark as blocked" >&2
    echo "  <blocker> — the issue number that blocks it" >&2
    exit 1
fi

ISSUE="${1#\#}"
BLOCKER="${2#\#}"
REPO="judgemind/judgemind"

# Validate arguments are numbers
if ! [[ "$ISSUE" =~ ^[0-9]+$ ]] || ! [[ "$BLOCKER" =~ ^[0-9]+$ ]]; then
    echo "Error: Both arguments must be issue numbers (digits only)." >&2
    exit 1
fi

# Fetch current issue body
BODY=$(gh issue view "$ISSUE" --repo "$REPO" --json body -q '.body' 2>/dev/null) || {
    echo "Error: Could not fetch issue #$ISSUE from $REPO." >&2
    exit 1
}

# Check if already blocked by this exact issue
if echo "$BODY" | grep -qE "^Blocked by #${BLOCKER}\b"; then
    vlog "Issue #$ISSUE already has 'Blocked by #$BLOCKER' in body — skipping body update."
else
    # Check if ## Dependencies section exists
    if echo "$BODY" | grep -q "^## Dependencies"; then
        # Append the Blocked by line after the ## Dependencies heading
        UPDATED_BODY=$(echo "$BODY" | awk -v line="Blocked by #$BLOCKER" '
            /^## Dependencies/ {
                print
                getline
                # Print any existing blank line after the heading
                if ($0 == "") {
                    print
                    getline
                }
                print line
                print
                next
            }
            { print }
        ')
    else
        # Append a new ## Dependencies section at the end
        UPDATED_BODY="${BODY}

## Dependencies

Blocked by #${BLOCKER}"
    fi

    # Write to a temp file and update the issue body
    TMPFILE=$(mktemp)
    echo "$UPDATED_BODY" > "$TMPFILE"
    gh issue edit "$ISSUE" --repo "$REPO" --body-file "$TMPFILE" > /dev/null
    rm -f "$TMPFILE"
    vlog "Added 'Blocked by #$BLOCKER' to issue #$ISSUE body."
fi

# Add status/blocked and remove agent/ready labels
gh issue edit "$ISSUE" --repo "$REPO" --add-label "status/blocked" --remove-label "agent/ready" > /dev/null 2>/dev/null || {
    # If agent/ready doesn't exist, try just adding status/blocked
    gh issue edit "$ISSUE" --repo "$REPO" --add-label "status/blocked" > /dev/null 2>/dev/null || true
}
vlog "Labels updated on issue #$ISSUE: +status/blocked, -agent/ready."

echo "Blocked #$ISSUE by #$BLOCKER."
