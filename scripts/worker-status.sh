#!/usr/bin/env bash
# Display a summary table of all active worker worktrees showing branch,
# PR, linked issue, CI status, and agent status file info.
#
# Usage: scripts/worker-status.sh
#
# Safe to run from anywhere inside the repo.

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve repo root (works from main repo or from inside a worktree)
# ---------------------------------------------------------------------------
GIT_DIR=$(git rev-parse --absolute-git-dir 2>/dev/null) || {
    echo "ERROR: not inside a git repository" >&2
    exit 1
}
REPO_ROOT="${GIT_DIR%%/.git*}"

# ---------------------------------------------------------------------------
# Collect active worker worktrees
# ---------------------------------------------------------------------------
# Parse porcelain output into "workerNum branchName" lines.
# Only considers worktrees matching */worktrees/worker-N.
# Uses POSIX-compatible awk (no gawk-specific features).
WORKERS=$(
    git -C "$REPO_ROOT" worktree list --porcelain \
        | awk '
            /^worktree / {
                path = $2
                num = ""
                wt_path = ""
                # Match paths ending in /worktrees/worker-<digits>
                n = split(path, parts, "/")
                if (n >= 2 && parts[n-1] == "worktrees") {
                    candidate = parts[n]
                    # Strip "worker-" prefix and check remainder is numeric
                    sub(/^worker-/, "", candidate)
                    if (candidate ~ /^[0-9]+$/) {
                        num = candidate
                        wt_path = path
                    }
                }
            }
            /^branch / {
                if (num != "") {
                    branch = substr($2, 12)   # strip "refs/heads/"
                    print num, branch, wt_path
                }
            }
        '
)

if [[ -z "$WORKERS" ]]; then
    echo "No active worker worktrees found."
    exit 0
fi

# ---------------------------------------------------------------------------
# Print header
# ---------------------------------------------------------------------------
printf "%-8s %-45s %-7s %-7s %-15s %s\n" \
    "Worker" "Branch" "PR" "Issue" "CI" "Agent Phase"
printf "%-8s %-45s %-7s %-7s %-15s %s\n" \
    "------" "------" "---" "-----" "---" "-----------"

# ---------------------------------------------------------------------------
# Build output lines, then sort by worker number
# ---------------------------------------------------------------------------
OUTPUT=""

while read -r worker_num branch worker_path; do
    pr_num="—"
    issue_num="—"
    ci_status="—"
    agent_phase="—"

    # --- Agent status file (lives inside the worktree) ---
    status_file="$worker_path/tmp/agent-status.txt"
    if [[ -f "$status_file" ]]; then
        agent_phase=$(awk -F': ' '/^phase:/ { print $2 }' "$status_file")
        file_issue=$(awk -F': ' '/^issue:/ { print $2 }' "$status_file")
        if [[ -n "$file_issue" ]]; then
            issue_num="$file_issue"
        fi
    fi

    # --- Open PR for this branch ---
    pr_json=$(gh pr list --repo judgemind/judgemind \
        --head "$branch" --state open \
        --json number,body --limit 1 2>/dev/null || echo "[]")

    pr_number=$(echo "$pr_json" | grep -oE '"number":[0-9]+' | head -1 | grep -oE '[0-9]+' || true)

    if [[ -n "$pr_number" && "$pr_number" != "0" ]]; then
        pr_num="#${pr_number}"

        # Extract linked issue from PR body (looks for "Closes #N")
        linked_issue=$(echo "$pr_json" | grep -oE 'Closes #[0-9]+' | head -1 | grep -oE '[0-9]+' || true)
        if [[ -n "$linked_issue" ]]; then
            issue_num="#${linked_issue}"
        fi

        # CI status from statusCheckRollup. ``mergeable`` and
        # ``mergeStateStatus`` are also fetched so the canonical
        # classifier (scripts/dispatcher/ci_classifier_cli.py — #4417)
        # can apply rules 3-4 (DIRTY ⇒ red, UNSTABLE ⇒ pending) and
        # not just count individual check conclusions.
        ci_json=$(gh pr view "$pr_number" --repo judgemind/judgemind \
            --json statusCheckRollup,mergeable,mergeStateStatus \
            2>/dev/null || echo "{}")

        # Single source of truth for "what counts as red / green /
        # pending" — delegate to the canonical Python classifier.
        # ``CANCELLED`` is non-blocking (#4414); ``STALE`` is treated
        # as green (matches the agent-runner's pre-refactor jq).
        # Pre-#4417 this was a bespoke awk regex that diverged from
        # the Python rule on STALE handling and required parallel
        # fixes for #4407 / #4414. Mapping: green→green, red→failed,
        # pending→running, error→pending (treat unparseable rollup as
        # transient rather than a hard failure).
        ci_verdict=$(echo "$ci_json" \
            | python3 "$REPO_ROOT/scripts/dispatcher/ci_classifier_cli.py" \
            2>/dev/null || echo "error")
        case "$ci_verdict" in
            green) ci_status="green" ;;
            red) ci_status="failed" ;;
            pending) ci_status="running" ;;
            *) ci_status="pending" ;;
        esac
    else
        ci_status="No PR"
    fi

    line=$(printf "%-8s %-45s %-7s %-7s %-15s %s" \
        "$worker_num" "$branch" "$pr_num" "$issue_num" "$ci_status" "$agent_phase")

    if [[ -z "$OUTPUT" ]]; then
        OUTPUT="$line"
    else
        OUTPUT="${OUTPUT}
${line}"
    fi

done <<< "$WORKERS"

# Sort output by worker number (first field, numeric)
echo "$OUTPUT" | sort -k1 -n

worker_count=$(echo "$WORKERS" | wc -l | tr -d ' ')
echo ""
echo "Workers: $worker_count"
