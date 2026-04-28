#!/usr/bin/env bash
# unblock-issue.sh — Manually unblock a single issue by pruning stale closed-blocker lines.
#
# For a given issue <N>, fetches its body and labels, checks each "Blocked by #M"
# line, and removes lines for blockers that are now closed.  If ALL blockers are
# resolved and the issue carries status/blocked, flips to agent/ready.
#
# This is the complement to unblock-dependents.sh (which operates in the opposite
# direction — given a closed issue, it finds every issue blocked by it).  Use
# unblock-issue.sh when you want to manually clean up a specific blocked issue
# rather than triggering the auto-unblock flow.
#
# Safety guarantees:
#   - Only prunes lines for CLOSED blockers; open-blocker lines are never removed.
#   - The status/blocked → agent/ready label flip requires ALL blockers to be closed.
#   - --dry-run shows intended actions without writing anything.
#
# Usage:
#   scripts/unblock-issue.sh <issue>
#   scripts/unblock-issue.sh 1939
#   scripts/unblock-issue.sh --dry-run 1939
#   scripts/unblock-issue.sh --help
#
# Options:
#   --dry-run   Show what would be done without making changes
#   --help      Show this help message

set -euo pipefail

DRY_RUN=false

# Parse options
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            echo "Usage: scripts/unblock-issue.sh [--dry-run] <issue>"
            echo ""
            echo "Manually unblock a single issue by pruning stale closed-blocker lines."
            echo ""
            echo "For each 'Blocked by #M' line in the issue body:"
            echo "  1. Check if blocker #M is closed"
            echo "  2. If closed: remove the 'Blocked by #M' line from the body"
            echo "  3. If the Dependencies section is now empty, remove it too"
            echo ""
            echo "Label flip (status/blocked -> agent/ready) only fires when ALL"
            echo "blockers are closed.  Open-blocker lines are never removed."
            echo ""
            echo "Options:"
            echo "  --dry-run   Show what would be done without making changes"
            echo "  --help      Show this help message"
            exit 0
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            echo "Run 'scripts/unblock-issue.sh --help' for usage." >&2
            exit 1
            ;;
        *)
            break
            ;;
    esac
done

if [ $# -ne 1 ]; then
    echo "Usage: scripts/unblock-issue.sh [--dry-run] <issue>" >&2
    echo "Run 'scripts/unblock-issue.sh --help' for full usage." >&2
    exit 1
fi

ISSUE="${1#\#}"
REPO="judgemind/judgemind"

# Validate argument is a number
if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
    echo "Error: Argument must be an issue number (digits only)." >&2
    exit 1
fi

if [ "$DRY_RUN" = true ]; then
    echo "[DRY RUN] Would unblock issue #$ISSUE"
    echo ""
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_TMPDIR="${SCRIPT_DIR}/../tmp"
mkdir -p "$WORK_TMPDIR"

# Fetch issue body and labels into a temp file (avoids shell quoting issues with
# issue bodies that contain special characters — same pattern as unblock-dependents.sh)
DATA_FILE="$WORK_TMPDIR/_unblock_issue_data_${ISSUE}.json"
echo "Fetching issue #$ISSUE..."
gh issue view "$ISSUE" --repo "$REPO" --json body,labels > "$DATA_FILE" 2>/dev/null || {
    rm -f "$DATA_FILE"
    echo "Error: Could not fetch issue #$ISSUE from $REPO." >&2
    exit 1
}

# Delegate to Python helper for JSON parsing and body manipulation.
# Pass the raw JSON file path via env var; the helper reads it directly.
REPO="$REPO" \
ISSUE="$ISSUE" \
DRY_RUN="$DRY_RUN" \
DATA_FILE="$DATA_FILE" \
WORK_TMPDIR="$WORK_TMPDIR" \
python3 "$SCRIPT_DIR/_unblock_issue.py"

EXIT_CODE=$?
rm -f "$DATA_FILE"
exit $EXIT_CODE
