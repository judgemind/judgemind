#!/usr/bin/env bash
# SessionEnd hook: remove the worktree for this session if it was running inside one.
#
# Claude Code passes a JSON object on stdin with at minimum:
#   { "cwd": "/path/to/working/dir", "session_id": "...", ... }
#
# If cwd is inside worktrees/worker-N we:
#   1. Derive the repo root
#   2. Run `git worktree prune` to clear any stale references
#   3. Remove the worktree so the worker number is freed for the next session

set -euo pipefail

input=$(cat)
cwd=$(printf '%s' "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))")

# Match paths of the form .../worktrees/worker-N or .../worktrees/worker-N/subdir
if [[ "$cwd" =~ ^(.+)/worktrees/(worker-[0-9]+)(/.*)?$ ]]; then
    repo_root="${BASH_REMATCH[1]}"
    worker_dir="$repo_root/worktrees/${BASH_REMATCH[2]}"

    # Prune stale references first (idempotent)
    git -C "$repo_root" worktree prune 2>/dev/null || true

    if [ -d "$worker_dir" ]; then
        git -C "$repo_root" worktree remove --force "$worker_dir" 2>/dev/null || true
        echo "session-end hook: removed worktree $worker_dir" >&2
    fi
fi
