#!/usr/bin/env bash
# Remove a worker worktree without deleting its branch (the branch backs an
# open PR and must stay until the PR is merged).
#
# Usage: scripts/end-worker.sh <worktree-path>
#   where <worktree-path> is the value printed by start-worker.sh
#
# Example: scripts/end-worker.sh /path/to/worktrees/worker-2

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

git -C "$REPO_ROOT" worktree remove "$WORKTREE" --force
echo "Removed worktree: $WORKTREE" >&2
echo "(Branch is preserved for the open PR.)" >&2
