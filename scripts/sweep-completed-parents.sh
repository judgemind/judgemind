#!/usr/bin/env bash
# sweep-completed-parents.sh — Auto-close parent meta-tasks whose Path-C
# sub-tasks all closed >= 24h ago.
#
# When an issue is decomposed via Path C into N sub-tasks (each carrying
# `Parent: #<N>` in the body, per docs/agent/task-dependencies.md §Sub-tasks),
# closing all sub-tasks does NOT auto-close the parent — `Parent:` is hierarchy,
# not a `Blocked by` dependency, so `unblock-dependents.sh` does not act on it.
# Result: the parent stays open with `status/in-progress` and re-enters the
# `agent/ready` queue days later, where a fresh agent runs a verify-and-close
# pivot to do what should have happened automatically.
#
# This script closes that loop. For every open issue whose body lists
# `Parent: #<N>` children:
#   1. Find every open or closed child via the `Parent: #<N>` body search.
#   2. Require >=1 child exists and ALL of them are CLOSED with
#      state_reason != "not_planned".
#   3. Require the most-recent child `closedAt` is >= 24 hours ago (grace
#      window — gives humans time to reopen if the parent's ACs weren't
#      really covered).
#   4. Skip if the parent has comments newer than the most-recent child
#      `closedAt` (suggests human follow-up is still in flight).
#   5. Otherwise: post an auto-close comment, close the parent with
#      `--reason completed`, and run `unblock-dependents.sh` on it so any
#      issues that were `Blocked by #<parent>` get unblocked too.
#
# Issue #4499 — see issue body for the full motivation and #4097 as the
# canonical recurrence (closed via verify-and-close 3 days late).
#
# Usage:
#   scripts/sweep-completed-parents.sh
#   scripts/sweep-completed-parents.sh --dry-run
#   scripts/sweep-completed-parents.sh --grace-hours 48
#   scripts/sweep-completed-parents.sh --help
#
# Options:
#   --dry-run           Show what would be done without making changes.
#   --grace-hours <H>   Override the 24-hour grace window (default: 24).
#   --limit <N>         Cap the number of candidate parents inspected (default: 200).
#   --help              Show this help message.
#
# Exit codes:
#   0 — completed without error (zero or more parents auto-closed).
#   1 — argument or environment error.
#   2 — gh CLI invocation failed (rate limit, auth, transient).

set -euo pipefail

DRY_RUN=false
GRACE_HOURS=24
LIMIT=200

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --grace-hours)
            if [[ $# -lt 2 ]]; then
                echo "Error: --grace-hours requires an integer argument" >&2
                exit 1
            fi
            GRACE_HOURS="$2"
            shift 2
            ;;
        --limit)
            if [[ $# -lt 2 ]]; then
                echo "Error: --limit requires an integer argument" >&2
                exit 1
            fi
            LIMIT="$2"
            shift 2
            ;;
        --help|-h)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            echo "Run 'scripts/sweep-completed-parents.sh --help' for usage." >&2
            exit 1
            ;;
        *)
            echo "Error: Unexpected positional argument: $1" >&2
            exit 1
            ;;
    esac
done

# Validate numeric args.
if ! [[ "$GRACE_HOURS" =~ ^[0-9]+$ ]]; then
    echo "Error: --grace-hours must be a non-negative integer" >&2
    exit 1
fi
if ! [[ "$LIMIT" =~ ^[0-9]+$ ]] || [[ "$LIMIT" -lt 1 ]]; then
    echo "Error: --limit must be a positive integer" >&2
    exit 1
fi

REPO="${REPO:-judgemind/judgemind}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_TMPDIR="${SCRIPT_DIR}/../tmp"
mkdir -p "$WORK_TMPDIR"

if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY RUN] Sweeping completed-parent candidates in $REPO (grace=${GRACE_HOURS}h)"
else
    echo "Sweeping completed-parent candidates in $REPO (grace=${GRACE_HOURS}h)"
fi

# Find open issues whose body contains "Parent: #<N>" — these are CHILDREN of
# some parent. The parent set is derived from the children's Parent: lines.
# We bound the result at $LIMIT candidate children.
echo "Listing open + closed sub-task children with 'Parent: #' in body..."

CHILDREN_JSON_OPEN="$WORK_TMPDIR/_sweep_children_open.json"
CHILDREN_JSON_CLOSED="$WORK_TMPDIR/_sweep_children_closed.json"

# We need both open and closed children for accurate "all closed?" checks.
# Search for the literal `Parent: ` in the body — narrow enough that GitHub
# search returns only sub-tasks. Broaden via two queries (open + closed).
gh issue list --repo "$REPO" \
    --state all \
    --search 'in:body "Parent: #"' \
    --json number,state,closedAt,body \
    --limit "$LIMIT" > "$CHILDREN_JSON_OPEN" 2>/dev/null || {
    echo "Error: gh issue list failed (search for 'Parent: #' children)" >&2
    rm -f "$CHILDREN_JSON_OPEN" "$CHILDREN_JSON_CLOSED"
    exit 2
}

# Delegate the rest (parent grouping, child-state checks, comment-since-close
# checks, action) to the Python helper. Keep the bash entry point small.
# `export` rather than `VAR=val cmd` form so the python3 path expansion below
# uses the parent's $SCRIPT_DIR (shellcheck SC2097/SC2098 false-positive
# avoidance — the prefix-assignment form would technically work but reads
# as ambiguous).
export REPO
export DRY_RUN
export GRACE_HOURS
export CHILDREN_FILE="$CHILDREN_JSON_OPEN"
export WORK_TMPDIR
export SCRIPT_DIR
set +e
python3 "$SCRIPT_DIR/_sweep_completed_parents.py"
RC=$?
set -e

rm -f "$CHILDREN_JSON_OPEN" "$CHILDREN_JSON_CLOSED"
exit "$RC"
