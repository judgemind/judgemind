#!/usr/bin/env bash
# Resolve repo root, clean stale worktrees, claim the next available worker
# number, create a worktree, and print its absolute path on stdout.
#
# Usage: scripts/start-worker.sh
# Output: absolute path to the new worktree (e.g. /path/to/worktrees/worker-2)
#
# Safe to run from anywhere inside the repo, including from an existing worktree.

set -euo pipefail

# ---------------------------------------------------------------------------
# Step 0 — Resolve repo root
# Works whether we're in the main repo or an existing worktree.
# ---------------------------------------------------------------------------
GIT_DIR=$(git rev-parse --absolute-git-dir 2>/dev/null) || {
    echo "ERROR: not inside a git repository" >&2
    exit 1
}
REPO_ROOT="${GIT_DIR%%/.git*}"

# ---------------------------------------------------------------------------
# Step 1a — Ensure main is up to date
# Skip pull if the working tree is dirty (uncommitted changes present).
# In that case the caller is responsible for carrying changes into the worktree.
# ---------------------------------------------------------------------------
git -C "$REPO_ROOT" checkout main --quiet
if git -C "$REPO_ROOT" diff --quiet && git -C "$REPO_ROOT" diff --cached --quiet; then
    git -C "$REPO_ROOT" pull origin main --quiet
else
    echo "WARNING: main has uncommitted changes — skipping pull. Copy your changes into the worktree manually." >&2
fi

# ---------------------------------------------------------------------------
# Step 1b — Prune stale worktree metadata
# ---------------------------------------------------------------------------
git -C "$REPO_ROOT" worktree prune

# ---------------------------------------------------------------------------
# Step 1c — Remove abandoned worktrees
#
# A worktree is stale if either:
#   1. Its branch is already merged into main (work is done), OR
#   2. Its branch name contains a session date older than today
#      (pattern: worker-N/session-YYYYMMDD-HHMM)
# ---------------------------------------------------------------------------
TODAY=$(date +%Y%m%d)

# Build a list of merged branch names (one per line, no leading spaces)
MERGED=$(git -C "$REPO_ROOT" branch --merged main | sed 's/^[* ]*//')

# Parse worktree list --porcelain into "<path> <branch>" lines.
# "refs/heads/" is 11 chars; substr($2, 12) strips it.
while read -r wt_path branch; do
    # Only consider worker worktrees (skip main repo and smoketest etc.)
    [[ "$wt_path" == */worktrees/worker-* ]] || continue

    stale=false

    # Check 1: branch merged into main
    if echo "$MERGED" | grep -qxF "$branch"; then
        stale=true
    fi

    # Check 2: session branch from a previous day
    if [[ "$branch" =~ /session-([0-9]{8})- ]]; then
        branch_date="${BASH_REMATCH[1]}"
        if [[ "$branch_date" < "$TODAY" ]]; then
            stale=true
        fi
    fi

    if [[ "$stale" == true ]]; then
        echo "Removing stale worktree: $wt_path (branch: $branch)" >&2
        git -C "$REPO_ROOT" worktree remove "$wt_path" --force
    fi
done < <(
    git -C "$REPO_ROOT" worktree list --porcelain \
        | awk '/^worktree / { path=$2 } /^branch / { print path, substr($2, 12) }'
)

# ---------------------------------------------------------------------------
# Step 2 — Claim the lowest available worker number and create the worktree
#
# Retries up to 10 times in case of a race with another agent.
# ---------------------------------------------------------------------------
for attempt in $(seq 1 10); do
    git -C "$REPO_ROOT" worktree prune

    # Extract existing worker numbers from worktree paths
    EXISTING=$(
        git -C "$REPO_ROOT" worktree list --porcelain \
            | awk '/^worktree / && $2 ~ /\/worktrees\/worker-[0-9]+/ {
                n = $2
                sub(/.*\/worker-/, "", n)
                print n + 0
            }' \
            | sort -n
    )

    # Find the lowest N >= 1 not already taken
    N=1
    while echo "$EXISTING" | grep -qx "$N"; do
        N=$((N + 1))
    done

    TIMESTAMP=$(date +%Y%m%d-%H%M)
    BRANCH="worker-${N}/session-${TIMESTAMP}"
    WORKTREE="$REPO_ROOT/worktrees/worker-${N}"

    if git -C "$REPO_ROOT" worktree add "$WORKTREE" -b "$BRANCH" 2>/dev/null; then
        git -C "$WORKTREE" config core.hooksPath .githooks
        mkdir -p "$WORKTREE/tmp"
        echo "$WORKTREE"
        exit 0
    fi

    echo "Worker $N was claimed concurrently, retrying... (attempt $attempt)" >&2
    sleep 0.5
done

echo "ERROR: failed to claim a worker number after 10 attempts" >&2
exit 1
