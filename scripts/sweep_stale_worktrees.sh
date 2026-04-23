#!/usr/bin/env bash
# sweep_stale_worktrees.sh — Proactively sweep stale worktree metadata.
#
# Scans .git/worktrees/<name>/ entries and removes metadata dirs for
# entries whose on-disk worktree path is missing. Complements the reactive
# fix in cleanup_worktree.sh (#2388), which only heals metadata for paths
# that something specifically invokes cleanup for.
#
# This is intended to be run at dispatcher startup so orphan metadata
# entries do not silently accumulate across sessions. Silent accumulation
# previously caused `git worktree list` to balloon with dead entries —
# 18 of them had to be cleaned by hand on the 2026-04-16 session 7 start.
#
# Usage:
#   scripts/sweep_stale_worktrees.sh           # sweep (human-readable log)
#   scripts/sweep_stale_worktrees.sh --quiet   # emit only the count line
#   scripts/sweep_stale_worktrees.sh --dry-run # report without removing
#
# Policy:
#   * Only remove metadata whose on-disk worktree dir is provably gone.
#   * Entries whose on-disk dir still exists are left untouched, even if
#     `locked` (same policy as cleanup_worktree.sh from #2388 — missing
#     on-disk dir means the worktree is definitely dead).
#   * Logs a `Cleaned up N stale worktree entries` summary line regardless
#     of N, so the dispatcher can surface it and detect trends.
#
# Exit codes:
#   0 — success (0 or more entries removed)
#   1 — error (could not find repo root, could not access metadata dir)

set -euo pipefail

# --- Flags ---
QUIET=0
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --quiet|-q)
            QUIET=1
            ;;
        --dry-run|-n)
            DRY_RUN=1
            ;;
        --help|-h)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//' | head -n -1
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [--quiet] [--dry-run]" >&2
            exit 1
            ;;
    esac
done

log() {
    if [[ "$QUIET" -eq 0 ]]; then
        echo "$@" >&2
    fi
}

# --- Find repo root ---
# Walk up from this script's location, same logic as cleanup_worktree.sh.
# We deliberately don't use `git rev-parse --show-toplevel` because inside
# a worktree it returns the worktree path, not the main repo root.
find_repo_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/CLAUDE.md" && -d "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "ERROR: cannot find repo root" >&2
    return 1
}

# --- Sweep ---
# For each entry in .git/worktrees/<name>/:
#   read the gitdir pointer file
#   if the worktree path it implies does not exist on disk -> rm -rf metadata
# Entries whose dir still exists are left alone (live or mid-creation).
sweep() {
    local repo_root="$1"
    local metadata_root="$repo_root/.git/worktrees"

    if [[ ! -d "$metadata_root" ]]; then
        # No worktrees metadata at all — nothing to sweep.
        echo "Cleaned up 0 stale worktree entries"
        return 0
    fi

    local removed=0
    local entry
    for entry in "$metadata_root"/*/; do
        # If the glob didn't match anything, $entry is the literal pattern.
        [[ -d "$entry" ]] || continue

        local name gitdir_file gitdir_target worktree_dir
        name="$(basename "$entry")"
        gitdir_file="$entry/gitdir"

        if [[ ! -f "$gitdir_file" ]]; then
            # Corrupt entry with no gitdir pointer — treat as stale.
            log "Stale (no gitdir pointer): $name"
            if [[ "$DRY_RUN" -eq 0 ]]; then
                rm -rf "$entry"
            fi
            removed=$((removed + 1))
            continue
        fi

        # gitdir points to the worktree's inner .git file; the worktree
        # dir is the parent of that path.
        gitdir_target="$(head -1 "$gitdir_file")"
        worktree_dir="$(dirname "$gitdir_target")"

        if [[ -d "$worktree_dir" ]]; then
            # On-disk worktree still exists — leave it alone, even if locked.
            continue
        fi

        # On-disk worktree is gone — orphan metadata.
        log "Stale (worktree dir gone): $name -> $worktree_dir"
        if [[ "$DRY_RUN" -eq 0 ]]; then
            rm -rf "$entry"
        fi
        removed=$((removed + 1))
    done

    # Summary line — always emitted, even with --quiet, so the dispatcher
    # can parse it. The dispatcher matches the literal "Cleaned up N stale
    # worktree entries" prefix.
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "Cleaned up $removed stale worktree entries (dry-run)"
    else
        echo "Cleaned up $removed stale worktree entries"
    fi
    return 0
}

main() {
    local repo_root
    repo_root="$(find_repo_root)" || return 1
    sweep "$repo_root"
}

main "$@"
