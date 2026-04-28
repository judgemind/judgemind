#!/usr/bin/env bash
# check-rebase-no-silent-drop.sh — Detect silent marker drops in a
# git merge-tree of HEAD against origin/main for watched files.
#
# 'Appended-list' files (e.g. scripts/tests/test_agent_runner_entrypoint.sh)
# are extended by multiple parallel agents. When two branches both append
# markers near the end of the file, git merge-tree can silently drop one
# side's additions without emitting a conflict marker. This check detects
# that case so the problem surfaces as a CI failure rather than as a
# missing test.
#
# Strategy (Option A): use `git merge-tree --write-tree origin/main HEAD`
# to compute an in-memory 3-way merge. For each watched file, compare the
# set of unique markers in HEAD, origin/main, and the merged tree. Any
# marker present in HEAD or origin/main but absent in the merged result —
# without a `<<<<<<<` conflict marker in the merged file — is a silent drop.
#
# Usage:
#   scripts/check-rebase-no-silent-drop.sh
#
# Exit codes:
#   0 — No silent drops detected (or origin/main is unreachable — skipped).
#   1 — One or more markers would be silently dropped by rebase.

# venv: none
# permanent: true

set -euo pipefail

# REPO_ROOT can be overridden via environment variable for testing.
: "${REPO_ROOT:=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# ─── Files to watch for silent drops ─────────────────────────────────────────
# Add entries here when a new 'appended-list' file is identified.
WATCH_FILES=(
    "scripts/tests/test_agent_runner_entrypoint.sh"
)

# ─── Marker pattern (ERE, passed to grep -E) ─────────────────────────────────
MARKER_PATTERN="^# Test T(_issue)?[0-9]+"

# ─── Check that origin/main is reachable ─────────────────────────────────────
if ! git -C "$REPO_ROOT" rev-parse --verify origin/main >/dev/null 2>&1; then
    echo "check-rebase-no-silent-drop: origin/main is unreachable (shallow clone or no remote) — skipping."
    exit 0
fi

# ─── Compute the merged tree ─────────────────────────────────────────────────
# git merge-tree --write-tree prints the tree SHA as the first line on stdout,
# optionally followed by conflict information on subsequent lines.
# It exits 0 for a clean merge and 1 when conflicts are present — we capture
# just the first line (the SHA) unconditionally.
merge_tree_output=$(git -C "$REPO_ROOT" merge-tree --write-tree origin/main HEAD 2>/dev/null || true)
merged_tree=$(printf '%s\n' "$merge_tree_output" | head -n 1)

if [ -z "$merged_tree" ]; then
    echo "check-rebase-no-silent-drop: could not compute merge-tree — skipping."
    exit 0
fi

violations=0

# ─── Check each watched file ─────────────────────────────────────────────────
for rel_file in "${WATCH_FILES[@]}"; do
    # Collect markers from HEAD
    head_markers=$(git -C "$REPO_ROOT" show "HEAD:$rel_file" 2>/dev/null \
        | grep -E "$MARKER_PATTERN" | sort -u || true)

    # Collect markers from origin/main
    main_markers=$(git -C "$REPO_ROOT" show "origin/main:$rel_file" 2>/dev/null \
        | grep -E "$MARKER_PATTERN" | sort -u || true)

    # Collect markers from the merged tree
    merged_content=$(git -C "$REPO_ROOT" show "${merged_tree}:${rel_file}" 2>/dev/null || true)
    merged_markers=$(printf '%s' "$merged_content" | grep -E "$MARKER_PATTERN" | sort -u || true)

    # Check for conflict markers in the merged file
    if printf '%s' "$merged_content" | grep -q '^<<<<<<<'; then
        # Conflict is visible — rebase will halt with a conflict; not a silent drop.
        continue
    fi

    # Union of markers from HEAD and origin/main
    all_expected=$(printf '%s\n%s\n' "$head_markers" "$main_markers" \
        | grep -v '^$' | sort -u || true)

    if [ -z "$all_expected" ]; then
        # No markers in either side — nothing to check.
        continue
    fi

    # Find any marker that is expected but absent from the merged result
    while IFS= read -r marker; do
        [ -z "$marker" ] && continue
        if ! printf '%s\n' "$merged_markers" | grep -qxF "$marker"; then
            if [ $violations -eq 0 ]; then
                echo "ERROR: git merge-tree would silently drop marker(s) when rebasing HEAD onto origin/main."
                echo ""
                echo "  This means a rebase of your branch onto main would lose test blocks"
                echo "  or other appended-list entries without producing a conflict. The"
                echo "  missing markers must be preserved."
                echo ""
                echo "  Fix: rebase interactively and ensure both sides' markers survive,"
                echo "  or restructure the conflicting appended-list file so git can"
                echo "  distinguish the two sets of additions."
                echo ""
            fi
            echo "    File:           $rel_file"
            echo "    Dropped marker: $marker"
            echo ""
            violations=$(( violations + 1 ))
        fi
    done < <(printf '%s\n' "$all_expected")
done

if [ $violations -gt 0 ]; then
    echo "  Found $violations silently-dropped marker(s)."
    echo ""
    exit 1
fi

echo "All clean — no silent marker drops detected."
exit 0
