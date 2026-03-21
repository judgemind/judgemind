#!/usr/bin/env bash
# Resolve repo root, clean stale worktrees, claim the next available worker
# number, create a worktree, and print its absolute path on stdout.
#
# Usage: scripts/start-worker.sh [--max-workers N]
# Output: absolute path to the new worktree (e.g. /path/to/worktrees/worker-2)
#
# Options:
#   --max-workers N   Refuse to create a worker if N or more already exist.
#                     Used by the dispatcher to enforce slot limits.
#
# Safe to run from anywhere inside the repo, including from an existing worktree.

set -euo pipefail

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
MAX_WORKERS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --max-workers)
            MAX_WORKERS="${2:?--max-workers requires a number}"
            if ! [[ "$MAX_WORKERS" =~ ^[1-9][0-9]*$ ]]; then
                echo "ERROR: --max-workers must be a positive integer, got '$MAX_WORKERS'" >&2
                exit 1
            fi
            shift 2
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "Usage: scripts/start-worker.sh [--max-workers N]" >&2
            exit 1
            ;;
    esac
done

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
# Step 1a — Fetch latest main from origin
# We intentionally do NOT checkout or pull in the parent repo — that would
# change the parent's working tree and interfere with other processes.
# Worktrees are created from origin/main directly (see Step 2).
# ---------------------------------------------------------------------------
git -C "$REPO_ROOT" fetch origin main --quiet

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

# Build a list of merged branch names (one per line, no leading spaces).
# Use origin/main so we don't depend on the local main branch being current.
MERGED=$(git -C "$REPO_ROOT" branch --merged origin/main | sed 's/^[* ]*//')

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
        # Check for a fresh agent lock before destroying the worktree.
        # A lock file younger than 30 minutes means an agent is actively
        # working — destroying it would wipe uncommitted changes (#1260).
        if [[ -f "$wt_path/.agent-lock" ]]; then
            lock_mtime=$(stat -f %m "$wt_path/.agent-lock" 2>/dev/null || stat -c %Y "$wt_path/.agent-lock" 2>/dev/null || echo 0)
            lock_age=$(( $(date +%s) - lock_mtime ))
            if [[ "$lock_age" -lt 1800 ]]; then
                echo "Skipping stale worktree $wt_path — agent lock is fresh (${lock_age}s old)" >&2
                continue
            fi
            echo "Agent lock in $wt_path is stale (${lock_age}s old) — proceeding with removal" >&2
        fi
        echo "Removing stale worktree: $wt_path (branch: $branch)" >&2
        git -C "$REPO_ROOT" worktree remove "$wt_path" --force
    fi
done < <(
    git -C "$REPO_ROOT" worktree list --porcelain \
        | awk '/^worktree / { path=$2 } /^branch / { print path, substr($2, 12) }'
)

# ---------------------------------------------------------------------------
# Step 1d — Clean stale worker directories and branches
#
# A worktree directory might exist without being registered (e.g. after a
# manual rm that left a partial directory). And local branches like
# worker-N/session-* might linger after a worktree was removed. Both of
# these block git worktree add from claiming the worker number.
# ---------------------------------------------------------------------------
WORKTREES_DIR="$REPO_ROOT/worktrees"

# Get the set of worker numbers that are registered as active worktrees
ACTIVE_WORKERS=$(
    git -C "$REPO_ROOT" worktree list --porcelain \
        | awk '/^worktree / && $2 ~ /\/worktrees\/worker-[0-9]+/ {
            n = $2
            sub(/.*\/worker-/, "", n)
            print n + 0
        }'
)

# Remove stale directories that aren't registered worktrees
if [ -d "$WORKTREES_DIR" ]; then
    for dir in "$WORKTREES_DIR"/worker-*; do
        [ -d "$dir" ] || continue
        dir_num=$(basename "$dir" | sed 's/worker-//')
        if ! echo "$ACTIVE_WORKERS" | grep -qx "$dir_num"; then
            # Check for a fresh agent lock before removing (#1254).
            # An unregistered directory with a fresh lock means an agent is
            # actively working — git metadata may have been pruned but the
            # agent's session is still alive.
            if [[ -f "$dir/.agent-lock" ]]; then
                lock_mtime=$(stat -f %m "$dir/.agent-lock" 2>/dev/null || stat -c %Y "$dir/.agent-lock" 2>/dev/null || echo 0)
                lock_age=$(( $(date +%s) - lock_mtime ))
                if [[ "$lock_age" -lt 1800 ]]; then
                    echo "Skipping unregistered directory $dir — agent lock is fresh (${lock_age}s old)" >&2
                    continue
                fi
            fi
            echo "Removing stale directory (not a registered worktree): $dir" >&2
            rm -rf "$dir"
        fi
    done
fi

# Delete stale local branches matching worker-N/* that have no active worktree
while IFS= read -r branch; do
    [ -n "$branch" ] || continue
    # Extract worker number from branch name (e.g. worker-3/session-... -> 3)
    branch_num=$(echo "$branch" | sed -n 's|^worker-\([0-9]*\)/.*|\1|p')
    [ -n "$branch_num" ] || continue
    if ! echo "$ACTIVE_WORKERS" | grep -qx "$branch_num"; then
        echo "Deleting stale local branch: $branch" >&2
        git -C "$REPO_ROOT" branch -D "$branch" 2>/dev/null || true
    fi
done < <(git -C "$REPO_ROOT" branch --list 'worker-*/*' | sed 's/^[* ]*//')

# ---------------------------------------------------------------------------
# Step 1e — Enforce max-workers limit (if set)
#
# Count currently active worker worktrees after pruning.  If the count
# already meets or exceeds --max-workers, refuse to create another.
# ---------------------------------------------------------------------------
if [[ -n "$MAX_WORKERS" ]]; then
    ACTIVE_COUNT=$(
        git -C "$REPO_ROOT" worktree list --porcelain \
            | awk '/^worktree / && $2 ~ /\/worktrees\/worker-[0-9]+/ { n++ } END { print n+0 }'
    )
    if [[ "$ACTIVE_COUNT" -ge "$MAX_WORKERS" ]]; then
        echo "ERROR: max workers reached ($ACTIVE_COUNT active, limit $MAX_WORKERS)" >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Step 2 — Claim the lowest available worker number and create the worktree
#
# Uses a PID-based lock file in {repo_root}/tmp/worker-locks/ to prevent
# two agents from claiming the same worker number concurrently (#1356).
# Retries up to 10 times in case of a race with another agent.
# ---------------------------------------------------------------------------
LOCK_DIR="$REPO_ROOT/tmp/worker-locks"
mkdir -p "$LOCK_DIR"

# Helper: check if a worker number is locked by an active process.
# Returns 0 (true) if locked, 1 (false) if not locked.
is_worker_locked() {
    local num="$1"
    local lock_file="$LOCK_DIR/worker-${num}.lock"
    if [[ ! -f "$lock_file" ]]; then
        return 1
    fi
    local locked_pid
    locked_pid=$(head -1 "$lock_file" 2>/dev/null | sed -n 's/^pid: *//p')
    if [[ -z "$locked_pid" ]]; then
        return 1
    fi
    # Check if the PID is still alive
    if kill -0 "$locked_pid" 2>/dev/null; then
        return 0
    fi
    # PID is dead — remove the stale lock file
    rm -f "$lock_file"
    return 1
}

for attempt in $(seq 1 10); do
    git -C "$REPO_ROOT" worktree prune

    # Extract existing worker numbers from registered worktree paths
    EXISTING=$(
        git -C "$REPO_ROOT" worktree list --porcelain \
            | awk '/^worktree / && $2 ~ /\/worktrees\/worker-[0-9]+/ {
                n = $2
                sub(/.*\/worker-/, "", n)
                print n + 0
            }' \
            | sort -n
    )

    # Also treat unregistered directories with a fresh agent lock as "taken"
    # so we never try to claim a worker number that another agent is using (#1254).
    if [ -d "$WORKTREES_DIR" ]; then
        for dir in "$WORKTREES_DIR"/worker-*; do
            [ -d "$dir" ] || continue
            dir_num=$(basename "$dir" | sed 's/worker-//')
            # Skip if already in the registered set
            if echo "$EXISTING" | grep -qx "$dir_num"; then
                continue
            fi
            # Check for a fresh agent lock
            if [[ -f "$dir/.agent-lock" ]]; then
                lock_mtime=$(stat -f %m "$dir/.agent-lock" 2>/dev/null || stat -c %Y "$dir/.agent-lock" 2>/dev/null || echo 0)
                lock_age=$(( $(date +%s) - lock_mtime ))
                if [[ "$lock_age" -lt 1800 ]]; then
                    EXISTING=$(printf '%s\n%s' "$EXISTING" "$dir_num" | sort -n)
                fi
            fi
        done
    fi

    # Also treat PID-locked worker numbers as "taken" (#1356).
    for lock_file in "$LOCK_DIR"/worker-*.lock; do
        [ -f "$lock_file" ] || continue
        lock_num=$(basename "$lock_file" | sed 's/^worker-//;s/\.lock$//')
        # Skip if already in the registered set
        if echo "$EXISTING" | grep -qx "$lock_num"; then
            continue
        fi
        if is_worker_locked "$lock_num"; then
            EXISTING=$(printf '%s\n%s' "$EXISTING" "$lock_num" | sort -n)
        fi
    done

    # Find the lowest N >= 1 not already taken
    N=1
    while echo "$EXISTING" | grep -qx "$N"; do
        N=$((N + 1))
    done

    TIMESTAMP=$(date +%Y%m%d-%H%M)
    BRANCH="worker-${N}/session-${TIMESTAMP}"
    WORKTREE="$REPO_ROOT/worktrees/worker-${N}"

    # Clean up any leftover directory or branch that could block creation.
    # At this point, $WORKTREE should not have a fresh lock (we skipped those
    # above), but double-check as a safety net (#1254).
    if [ -d "$WORKTREE" ]; then
        if [[ -f "$WORKTREE/.agent-lock" ]]; then
            lock_mtime=$(stat -f %m "$WORKTREE/.agent-lock" 2>/dev/null || stat -c %Y "$WORKTREE/.agent-lock" 2>/dev/null || echo 0)
            lock_age=$(( $(date +%s) - lock_mtime ))
            if [[ "$lock_age" -lt 1800 ]]; then
                echo "Skipping worker $N — agent lock is fresh (${lock_age}s old)" >&2
                continue
            fi
        fi
        echo "Removing leftover directory blocking worker $N: $WORKTREE" >&2
        rm -rf "$WORKTREE"
    fi
    # Delete any existing local branches for this worker number
    while IFS= read -r old_branch; do
        [ -n "$old_branch" ] || continue
        echo "Deleting leftover branch blocking worker $N: $old_branch" >&2
        git -C "$REPO_ROOT" branch -D "$old_branch" 2>/dev/null || true
    done < <(git -C "$REPO_ROOT" branch --list "worker-${N}/*" | sed 's/^[* ]*//')

    # Atomically claim this worker number via PID lock file (#1356).
    # Write our PID to the lock file. If another process wrote it first,
    # we detect that on the next iteration.
    LOCK_FILE="$LOCK_DIR/worker-${N}.lock"
    printf 'pid: %s\ntimestamp: %s\n' "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$LOCK_FILE"
    # Re-read to verify we own it (basic last-writer-wins check)
    claimed_pid=$(head -1 "$LOCK_FILE" 2>/dev/null | sed -n 's/^pid: *//p')
    if [[ "$claimed_pid" != "$$" ]]; then
        echo "Worker $N lock was taken by PID $claimed_pid, retrying... (attempt $attempt)" >&2
        sleep 0.5
        continue
    fi

    if git -C "$REPO_ROOT" worktree add "$WORKTREE" -b "$BRANCH" origin/main 2>/dev/null; then
        git -C "$WORKTREE" config core.hooksPath .githooks
        mkdir -p "$WORKTREE/tmp"
        # Create agent lock file to protect this worktree from being
        # destroyed by another agent or the dispatcher (#1260).
        date -u +%Y-%m-%dT%H:%M:%SZ > "$WORKTREE/.agent-lock"
        echo "$WORKTREE"
        exit 0
    fi

    # Worktree creation failed — release the PID lock and retry
    rm -f "$LOCK_FILE"
    echo "Worker $N was claimed concurrently, retrying... (attempt $attempt)" >&2
    sleep 0.5
done

echo "ERROR: failed to claim a worker number after 10 attempts" >&2
exit 1
