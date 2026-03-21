#!/usr/bin/env bash
# Remove a worker worktree without deleting its branch (the branch backs an
# open PR and must stay until the PR is merged).
#
# Usage: scripts/end-worker.sh <worktree-path> [--force]
#   where <worktree-path> is the value printed by start-worker.sh
#
# Options:
#   --force   Skip the agent lock check (use when you are the owning agent
#             removing your own worktree, or the dispatcher has confirmed
#             the agent has completed via task notification).
#
# Example: scripts/end-worker.sh /path/to/worktrees/worker-2
#          scripts/end-worker.sh /path/to/worktrees/worker-2 --force

set -euo pipefail

WORKTREE="${1:?Usage: end-worker.sh <worktree-path>}"

GIT_DIR=$(git rev-parse --absolute-git-dir 2>/dev/null) || {
    echo "ERROR: not inside a git repository" >&2
    exit 1
}
REPO_ROOT="${GIT_DIR%%/.git*}"

if [[ ! -d "$WORKTREE" ]]; then
    echo "ERROR: worktree directory not found: $WORKTREE" >&2
    exit 1
fi

# ── Check agent lock ────────────────────────────────────────────────────
# Refuse to destroy a worktree with a fresh lock file (< 30 min old).
# Pass --force to skip the lock check (for self-removal at task end).
FORCE=false
if [[ "${2:-}" == "--force" ]]; then
    FORCE=true
fi

if [[ "$FORCE" != true && -f "$WORKTREE/.agent-lock" ]]; then
    lock_mtime=$(stat -f %m "$WORKTREE/.agent-lock" 2>/dev/null || stat -c %Y "$WORKTREE/.agent-lock" 2>/dev/null || echo 0)
    lock_age=$(( $(date +%s) - lock_mtime ))
    if [[ "$lock_age" -lt 1800 ]]; then
        echo "ERROR: worktree $WORKTREE has a fresh agent lock (${lock_age}s old, < 30 min)." >&2
        echo "An agent is actively working here. Use --force to override." >&2
        exit 1
    fi
    echo "Agent lock in $WORKTREE is stale (${lock_age}s old) — proceeding with removal" >&2
fi

# cd to the repo root before removing the worktree so that bash's cwd is not
# inside the directory being deleted. Without this, bash emits a spurious
# "pwd: getcwd: No such file or directory" error and exits non-zero.
cd "$REPO_ROOT"
git worktree remove "$WORKTREE" --force
echo "Removed worktree: $WORKTREE" >&2
echo "(Branch is preserved for the open PR.)" >&2
