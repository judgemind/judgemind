#!/usr/bin/env bash
# venv: none
# one-off: true
#
# cleanup-stale-succeeded-rows.sh — Close GitHub issues for dispatcher agent
# rows that have status='succeeded' AND merged_at IS NOT NULL but whose
# originating GitHub issue is still OPEN.
#
# Background (#3738): After a PR merges, the daemon stamps ``merged_at`` on
# the agent row but GitHub sometimes does NOT auto-close the issue (when the
# PR body lacks a ``Closes #N`` keyword). This leaves stale succeeded rows
# that previously blocked the ready queue indefinitely via
# ``_issue_already_attempted``. Migration 57 removed the block at the SQL
# level; this script drains the backlog that accumulated before migration 57.
#
# Usage:
#   scripts/cleanup-stale-succeeded-rows.sh
#   scripts/cleanup-stale-succeeded-rows.sh --dry-run
#   scripts/cleanup-stale-succeeded-rows.sh --help
#
# Options:
#   --dry-run   Show what would be done without making any changes
#   --help      Show this help message
#
# Prerequisites:
#   - scripts/dev-db-query.sh must be available and configured
#   - gh CLI authenticated with access to judgemind/judgemind
#
# Idempotent: issues already CLOSED are silently skipped.

set -euo pipefail

DRY_RUN=false
REPO="judgemind/judgemind"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            echo "Usage: scripts/cleanup-stale-succeeded-rows.sh [--dry-run]"
            echo ""
            echo "Close GitHub issues for dispatcher agent rows with"
            echo "  status='succeeded' AND merged_at IS NOT NULL"
            echo "whose originating GitHub issue is still OPEN."
            echo ""
            echo "Options:"
            echo "  --dry-run   Show what would be done without making changes"
            echo "  --help      Show this help message"
            echo ""
            echo "See issue #3738 for background."
            exit 0
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            echo "Run 'scripts/cleanup-stale-succeeded-rows.sh --help' for usage." >&2
            exit 1
            ;;
        *)
            echo "Error: Unexpected argument: $1" >&2
            echo "Run 'scripts/cleanup-stale-succeeded-rows.sh --help' for usage." >&2
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$DRY_RUN" = true ]; then
    echo "[DRY RUN] Would close stale succeeded rows (no changes will be made)"
    echo ""
fi

# ---------------------------------------------------------------------------
# Query: fetch stale succeeded rows
# ---------------------------------------------------------------------------

echo "Querying dispatcher.agents for stale succeeded rows..."

STALE_JSON=$("$SCRIPT_DIR/dev-db-query.sh" \
    "SELECT issue_number, pr_number FROM dispatcher.agents WHERE status = 'succeeded' AND merged_at IS NOT NULL ORDER BY id" \
    --format json 2>/dev/null) || {
    echo "Error: dev-db-query.sh failed. Is the ECS tunnel / VPN configured?" >&2
    exit 1
}

ROW_COUNT=$(printf '%s' "$STALE_JSON" | python3 -c "import json, sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)

if [ "$ROW_COUNT" = "0" ] || [ -z "$ROW_COUNT" ]; then
    echo "No stale succeeded rows found. Nothing to do."
    exit 0
fi

echo "Found $ROW_COUNT stale succeeded row(s). Checking GitHub issue state..."
echo ""

# ---------------------------------------------------------------------------
# Process each row
# ---------------------------------------------------------------------------

CLOSED=0
SKIPPED=0
ERRORS=0

while IFS= read -r row; do
    ISSUE_NUMBER=$(printf '%s' "$row" | python3 -c "import json, sys; d=json.load(sys.stdin); print(d['issue_number'])")
    PR_NUMBER=$(printf '%s' "$row" | python3 -c "import json, sys; d=json.load(sys.stdin); v=d.get('pr_number'); print(v if v is not None else '')")

    # Check if the issue is still open on GitHub.
    ISSUE_STATE=$(gh issue view "$ISSUE_NUMBER" --repo "$REPO" --json state --jq '.state' 2>/dev/null || echo "ERROR")

    if [ "$ISSUE_STATE" = "ERROR" ]; then
        echo "  [ERROR] #$ISSUE_NUMBER — gh issue view failed; skipping"
        ERRORS=$((ERRORS + 1))
        continue
    fi

    if [ "$ISSUE_STATE" != "OPEN" ]; then
        echo "  [SKIP]  #$ISSUE_NUMBER — already $ISSUE_STATE"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Compose close comment.
    if [ -n "$PR_NUMBER" ]; then
        CLOSE_COMMENT="Closed by autonomous cleanup of stale succeeded row (see #3738; PR #${PR_NUMBER} already merged, but GitHub did not auto-close this issue because the PR body lacked a Closes keyword)."
    else
        CLOSE_COMMENT="Closed by autonomous cleanup of stale succeeded row (see #3738; PR already merged, but GitHub did not auto-close this issue)."
    fi

    if [ "$DRY_RUN" = true ]; then
        echo "  [DRY]   #$ISSUE_NUMBER — would close (PR #${PR_NUMBER:-unknown}) and remove agent/ready"
        CLOSED=$((CLOSED + 1))
        continue
    fi

    echo "  [CLOSE] #$ISSUE_NUMBER — closing (PR #${PR_NUMBER:-unknown})"

    gh issue close "$ISSUE_NUMBER" \
        --repo "$REPO" \
        --reason completed \
        --comment "$CLOSE_COMMENT" || {
        echo "  [ERROR] #$ISSUE_NUMBER — gh issue close failed; skipping label strip"
        ERRORS=$((ERRORS + 1))
        continue
    }

    gh issue edit "$ISSUE_NUMBER" \
        --repo "$REPO" \
        --remove-label "agent/ready" 2>/dev/null || true  # idempotent; label may not exist

    CLOSED=$((CLOSED + 1))
done < <(printf '%s' "$STALE_JSON" | python3 -c "
import json, sys
rows = json.load(sys.stdin)
for r in rows:
    print(json.dumps(r))
")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "Done."
if [ "$DRY_RUN" = true ]; then
    echo "  Would close: $CLOSED  |  Already closed (skip): $SKIPPED  |  Errors: $ERRORS"
else
    echo "  Closed: $CLOSED  |  Already closed (skip): $SKIPPED  |  Errors: $ERRORS"
fi

if [ "$ERRORS" -gt 0 ]; then
    exit 1
fi
