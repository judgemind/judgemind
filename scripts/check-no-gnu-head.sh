#!/usr/bin/env bash
# check-no-gnu-head.sh — Forbid `head -n -<N>` (negative line count) in
# scripts/tests/.
#
# `head -n -<N>` is a GNU coreutils extension that prints all lines except
# the last N.  It is not supported by BSD/macOS `head` (bash 3.2 default
# on macOS), which rejects it with "illegal line count".  This caused
# test_agent_runner_entrypoint.sh to fail on macOS (see issue #3648).
#
# The POSIX-portable replacement is `sed '$d'` (drop the last line) or
# `awk 'NR>1{print prev} {prev=$0}'` (drop the last N lines via accumulation).
#
# Default scan directory is `scripts/tests/` (not the repo root) per the
# AC3 verify scope in issue #3681 — test files are the highest-risk
# location for this pattern because they run in CI environments that may
# not have GNU coreutils.
#
# Usage:
#   scripts/check-no-gnu-head.sh               # scan scripts/tests/
#   scripts/check-no-gnu-head.sh [dir]         # scan a specific directory
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more files use `head -n -<N>` (negative count).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT/scripts/tests}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Pattern ─────────────────────────────────────────────────────────────
# Match `head` followed by optional whitespace, `-n`, optional whitespace,
# and a negative number (dash + digit).  This is the GNU-only form.
PATTERN='head[[:space:]]+-n[[:space:]]+-[0-9]'

# ─── Files that legitimately mention the pattern as context ──────────────
# The check script and its test must spell the pattern literally.
EXCLUDE_FILES=(
    "scripts/check-no-gnu-head.sh"
    "scripts/tests/test_check_no_gnu_head.sh"
)

# ─── Build --exclude-dir arguments from the canonical list ──────────────
# (REPO_WALK_EXCLUSIONS lives in preflight.sh — single source of truth, #4308.)
exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Scan the codebase ──────────────────────────────────────────────────

violations=0

matches=$(grep -rnE "$PATTERN" "$SCAN_DIR" "${exclude_args[@]}" 2>/dev/null || true)

if [[ -n "$matches" ]]; then
    while IFS= read -r line; do
        # grep -rn output: <path>:<lineno>:<content>
        file_path="${line%%:*}"
        rest="${line#*:}"
        content="${rest#*:}"

        # Skip matches in the explicitly-excluded files.
        skip=false
        for excl in "${EXCLUDE_FILES[@]}"; do
            if [[ "$file_path" == *"$excl" ]]; then
                skip=true
                break
            fi
        done
        if "$skip"; then
            continue
        fi

        # Skip comment lines (first non-whitespace character is `#`).
        # Covers shell, YAML, Python, and Dockerfile comments.
        if [[ "$content" =~ ^[[:space:]]*# ]]; then
            continue
        fi

        if [[ $violations -eq 0 ]]; then
            echo "ERROR: Found GNU-only 'head -n -<N>' usage in test files."
            echo ""
            echo "  'head -n -N' (negative count) is a GNU coreutils extension."
            echo "  It is not supported by BSD/macOS head (the default on macOS)"
            echo "  and fails with 'illegal line count' on Apple Silicon and"
            echo "  Intel macOS.  Use POSIX-compatible alternatives instead."
            echo ""
        fi

        echo "    $line"
        violations=$((violations + 1))
    done <<< "$matches"
fi

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of 'head -n -<N>'."
    echo ""
    echo "  Fix: replace with a POSIX-portable equivalent:"
    echo ""
    echo "    head -n -1 file       →  sed '\$d' file"
    echo "    head -n -1 file       →  awk 'NR>1{print prev} {prev=\$0}' file"
    echo "    head -n -N file       →  use awk with an accumulation buffer"
    echo ""
    echo "  The 'sed \$d' form drops the last line and is POSIX-standard."
    echo ""
    echo "  If the pattern must appear as explanatory context, put it in a"
    echo "  comment (lines starting with '#')."
    exit 1
fi

echo "All clean — no GNU-only 'head -n -<N>' usage detected."
exit 0
