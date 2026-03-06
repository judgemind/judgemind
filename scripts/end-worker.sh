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

# cd to the repo root before removing the worktree so that bash's cwd is not
# inside the directory being deleted. Without this, bash emits a spurious
# "pwd: getcwd: No such file or directory" error and exits non-zero.
cd "$REPO_ROOT"
git worktree remove "$WORKTREE" --force
echo "Removed worktree: $WORKTREE" >&2
echo "(Branch is preserved for the open PR.)" >&2
