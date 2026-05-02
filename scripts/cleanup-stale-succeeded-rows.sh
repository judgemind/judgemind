#!/usr/bin/env bash
# venv: none
# one-off: true
#
# cleanup-stale-succeeded-rows.sh — Close GitHub issues for dispatcher agent
# rows where status='succeeded' AND merged_at IS NOT NULL.
#
# Issue #3738.  After migration 57 the SQL gate for
# ``dispatcher.issue_has_active_agent`` ignores succeeded rows whose PR has
# already merged (merged_at IS NOT NULL).  This script closes the originating
# GitHub issues for those rows so they are removed from the agent/ready queue
# and don't resurface in subsequent queue scans.
#
# Idempotent — issues that are already CLOSED are silently skipped.
#
# Usage
# -----
#   scripts/cleanup-stale-succeeded-rows.sh           # live run
#   scripts/cleanup-stale-succeeded-rows.sh --dry-run # preview only, no writes
#   scripts/cleanup-stale-succeeded-rows.sh --help
#
# Prerequisites
# -------------
#   - AWS CLI v2 with Session Manager plugin (for dev-db-query.sh)
#   - gh CLI authenticated with judgemind/judgemind write access
#   - GITHUB_REPO env var (default: judgemind/judgemind)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GITHUB_REPO="${GITHUB_REPO:-judgemind/judgemind}"
DRY_RUN=false

usage() {
    cat >&2 <<'USAGE'
Usage: scripts/cleanup-stale-succeeded-rows.sh [--dry-run] [--help]

Close GitHub issues for dispatcher.agents rows where:
  status = 'succeeded' AND merged_at IS NOT NULL

Issue #3738: after migration 57 the SQL gate for
issue_has_active_agent ignores these rows. This script closes the
originating GitHub issues so they leave the agent/ready queue.

Flags:
  --dry-run   List affected rows and issue states without making
              any changes to GitHub.
  --help      Show this message and exit.
USAGE
}

# ─── Parse flags ──────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done

# ─── Query stale succeeded rows ───────────────────────────────────────────────

echo "Querying stale succeeded rows (status='succeeded' AND merged_at IS NOT NULL)..." >&2

rows_json=$(
    "$SCRIPT_DIR/dev-db-query.sh" \
        "SELECT issue_number, pr_number FROM dispatcher.agents WHERE status = 'succeeded' AND merged_at IS NOT NULL ORDER BY issue_number"
)

row_count=$(printf '%s' "$rows_json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(len(data))
")

echo "Found ${row_count} stale succeeded row(s)." >&2

if [[ "$row_count" -eq 0 ]]; then
    echo "Nothing to do." >&2
    exit 0
fi

if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY RUN] Would process the following rows:" >&2
    printf '%s\n' "$rows_json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for row in data:
    print(f\"  issue #{row['issue_number']}  pr #{row['pr_number']}\")
" >&2
    exit 0
fi

# ─── Process each row ─────────────────────────────────────────────────────────

closed=0
skipped=0
errors=0

tab_rows=$(printf '%s' "$rows_json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for row in data:
    print(str(row['issue_number']) + '\t' + str(row['pr_number']))
")

while IFS=$'\t' read -r issue_number pr_number; do
    echo "Processing issue #${issue_number} (PR #${pr_number})..." >&2

    # Check current issue state.
    issue_state=$(
        gh issue view "$issue_number" \
            --repo "$GITHUB_REPO" \
            --json state \
            --jq '.state' \
        2>/dev/null || echo "ERROR"
    )

    if [[ "$issue_state" == "ERROR" ]]; then
        echo "  ERROR: could not fetch state for issue #${issue_number} — skipping." >&2
        (( errors++ )) || true
        continue
    fi

    if [[ "$issue_state" != "OPEN" ]]; then
        echo "  SKIP: issue #${issue_number} is already ${issue_state}." >&2
        (( skipped++ )) || true
        continue
    fi

    # Issue is OPEN — close it with a comment naming the PR and issue #3738.
    close_comment="Closed by PR #${pr_number} (autonomous post-merge cleanup, see #3738 — this issue had a dispatcher agent row with status=succeeded and merged_at set, indicating the PR merged successfully but the issue was not closed)."

    gh issue close "$issue_number" \
        --repo "$GITHUB_REPO" \
        --comment "$close_comment" \
        --reason "completed" \
    && echo "  Closed issue #${issue_number}." >&2 \
    || { echo "  ERROR: failed to close issue #${issue_number}." >&2; (( errors++ )) || true; continue; }

    # Strip agent/ready label so the issue doesn't reappear in queue scans.
    gh issue edit "$issue_number" \
        --repo "$GITHUB_REPO" \
        --remove-label "agent/ready" \
    && echo "  Removed agent/ready label from issue #${issue_number}." >&2 \
    || echo "  WARNING: could not remove agent/ready label from issue #${issue_number} (may already be absent)." >&2

    (( closed++ )) || true

done <<< "$tab_rows"

# ─── Summary ──────────────────────────────────────────────────────────────────

echo "" >&2
echo "Done.  closed=${closed}  skipped=${skipped}  errors=${errors}" >&2

if [[ "$errors" -gt 0 ]]; then
    exit 1
fi
