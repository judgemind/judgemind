#!/usr/bin/env bash
# Refresh the agent lock file in the current worktree by updating its
# modification time.  This keeps the lock "fresh" (< 30 min) so that
# start-worker.sh and end-worker.sh do not treat the worktree as stale.
#
# Usage: scripts/refresh-agent-lock.sh [worktree-path]
#   If no path is given, the current git worktree is used.
#
# Safe to call frequently — it only touches one file.

set -euo pipefail

if [[ -n "${1:-}" ]]; then
    WORKTREE="$1"
else
    GIT_DIR=$(git rev-parse --absolute-git-dir 2>/dev/null) || {
        echo "ERROR: not inside a git repository" >&2
        exit 1
    }
    WORKTREE="${GIT_DIR%%/.git*}"
fi

LOCK_FILE="$WORKTREE/.agent-lock"

if [[ ! -f "$LOCK_FILE" ]]; then
    # Create the lock if it does not exist (idempotent).
    date -u +%Y-%m-%dT%H:%M:%SZ > "$LOCK_FILE"
else
    # Touch to update modification time.
    touch "$LOCK_FILE"
fi
