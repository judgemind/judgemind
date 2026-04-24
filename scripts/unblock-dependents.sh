#!/usr/bin/env bash
# unblock-dependents.sh — Unblock all issues that were blocked by a now-closed issue.
#
# Searches for open issues containing "Blocked by #N" in the body, checks if ALL
# blockers are resolved (closed), and if so: removes the Blocked by lines and empty
# Dependencies section from the body, removes the status/blocked label, and adds
# agent/ready.
#
# This is the complement to block-issue.sh. It automates the manual unblocking
# process described in docs/agent/task-dependencies.md §When you finish a task.
#
# Usage:
#   scripts/unblock-dependents.sh [--verbose|-v] <closed-issue>
#   scripts/unblock-dependents.sh 1939
#   scripts/unblock-dependents.sh --dry-run 1939
#   scripts/unblock-dependents.sh --help
#
# Options:
#   --dry-run       Show what would be done without making changes
#   --verbose, -v   Show detailed progress output (default: terse)
#   --help          Show this help message
#
# Environment:
#   JM_VERBOSE=1    Same as --verbose

set -euo pipefail

SCRIPT_DIR_UNBLOCK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_terse_lib.sh
source "$SCRIPT_DIR_UNBLOCK/_terse_lib.sh"

DRY_RUN=false

# Parse options
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --verbose|-v)
            VERBOSE=1
            shift
            ;;
        --help|-h)
            echo "Usage: scripts/unblock-dependents.sh [--dry-run] <closed-issue>"
            echo ""
            echo "Unblock all issues that were blocked by the given (now-closed) issue."
            echo ""
            echo "For each open issue with 'Blocked by #N' in the body:"
            echo "  1. Check if ALL blockers are resolved (closed)"
            echo "  2. If yes: remove Blocked by lines, clean up Dependencies section,"
            echo "     remove status/blocked label, add agent/ready label"
            echo "  3. If no: skip (other blockers still open)"
            echo ""
            echo "Options:"
            echo "  --dry-run   Show what would be done without making changes"
            echo "  --help      Show this help message"
            exit 0
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            echo "Run 'scripts/unblock-dependents.sh --help' for usage." >&2
            exit 1
            ;;
        *)
            break
            ;;
    esac
done

if [ $# -ne 1 ]; then
    echo "Usage: scripts/unblock-dependents.sh [--dry-run] <closed-issue>" >&2
    echo "Run 'scripts/unblock-dependents.sh --help' for full usage." >&2
    exit 1
fi

CLOSED_ISSUE="${1#\#}"
REPO="judgemind/judgemind"

# Validate argument is a number
if ! [[ "$CLOSED_ISSUE" =~ ^[0-9]+$ ]]; then
    echo "Error: Argument must be an issue number (digits only)." >&2
    exit 1
fi

if [ "$DRY_RUN" = true ]; then
    echo "[DRY RUN] Would unblock issues blocked by #$CLOSED_ISSUE"
    echo ""
fi

# Search for open issues containing "Blocked by #N"
vlog "Searching for open issues blocked by #$CLOSED_ISSUE..."
BLOCKED_ISSUES=$(gh issue list --repo "$REPO" \
    --state open \
    --search "\"Blocked by #$CLOSED_ISSUE\"" \
    --json number,body,labels \
    --limit 50 2>/dev/null) || {
    echo "Error: Could not search for blocked issues." >&2
    exit 1
}

# Check if any results
COUNT=$(echo "$BLOCKED_ISSUES" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null)
if [ "$COUNT" = "0" ] || [ -z "$COUNT" ]; then
    echo "No open issues found with 'Blocked by #$CLOSED_ISSUE' in the body."
    exit 0
fi

vlog "Found $COUNT candidate issue(s). Checking blockers..."

# Resolve the directory for temp files
WORK_TMPDIR="${SCRIPT_DIR_UNBLOCK}/../tmp"
mkdir -p "$WORK_TMPDIR"

# Delegate to Python helper for JSON parsing and body manipulation
REPO="$REPO" \
CLOSED_ISSUE="$CLOSED_ISSUE" \
DRY_RUN="$DRY_RUN" \
BLOCKED_ISSUES="$BLOCKED_ISSUES" \
WORK_TMPDIR="$WORK_TMPDIR" \
JM_VERBOSE="${JM_VERBOSE:-}" \
VERBOSE="${VERBOSE:-0}" \
python3 "$SCRIPT_DIR_UNBLOCK/_unblock_dependents.py"
