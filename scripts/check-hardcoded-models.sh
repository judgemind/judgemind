#!/usr/bin/env bash
# check-hardcoded-models.sh — Detect hardcoded Claude model identifiers.
#
# Enforces that Claude model identifiers are centralized in
# packages/judgemind-config/src/judgemind_config/models.py instead of
# being hardcoded throughout the codebase.  Catches regressions at PR
# time after the centralization done in #1115.
#
# Scans packages/*/src/ and scripts/*.py for patterns like
# "claude-haiku-4-5-20251001" or "claude-sonnet-4-20250514".
#
# Excluded by design:
#   - The judgemind-config models.py source of truth
#   - Test files (packages/*/tests/, scripts/tests/)
#   - Eval scripts (scripts/eval/)
#   - Comments and docstrings (lines starting with # or containing
#     documentation markers)
#   - SQL comments (-- ...)
#   - This script and its tests
#
# Usage:
#   scripts/check-hardcoded-models.sh          # exits 0 if clean, 1 if violations
#   scripts/check-hardcoded-models.sh [dir]    # scan a specific directory
#
# Exit codes:
#   0 — No hardcoded model identifiers found.
#   1 — One or more files contain hardcoded model strings.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Model identifier patterns ────────────────────────────────────────
# Match current and future Claude model IDs.  These patterns match
# dated model identifiers (e.g. claude-haiku-4-5-20251001) and
# undated aliases (e.g. claude-sonnet-4-6).
#
# The pattern requires a digit after the family name to avoid matching
# prose mentions of "claude-haiku" or "claude-sonnet" in comments.
PATTERN='claude-(haiku|sonnet|opus)-[0-9][a-zA-Z0-9._-]*'

# ─── Directories to exclude from scanning ─────────────────────────────
# Repo-walk dir exclusions come from REPO_WALK_EXCLUSIONS in preflight.sh
# (canonical list — single source of truth, see #4308). No per-check
# extras needed here.

# ─── Files to exclude (relative to repo root) ─────────────────────────
# The source-of-truth file, this script, its tests, and the deprecated
# models checker (which references current models in fix suggestions).
EXCLUDE_FILES=(
    "packages/judgemind-config/src/judgemind_config/models.py"
    "scripts/check-hardcoded-models.sh"
    "scripts/tests/test_check_hardcoded_models.sh"
    "scripts/check-deprecated-models.sh"
)

# ─── Path patterns to exclude ──────────────────────────────────────────
# Test directories and eval scripts are allowed to hardcode model IDs.
EXCLUDE_PATH_PATTERNS=(
    "*/tests/*"
    "*/test_*"
    "scripts/eval/*"
)

# ─── Build --exclude-dir arguments from the canonical list ─────────────
exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Determine scan targets ───────────────────────────────────────────
# When scanning the repo root, only look inside packages/*/src/ and
# scripts/*.py.  When a custom directory is passed (e.g. from tests),
# scan everything in it.

scan_targets=()
if [[ "$SCAN_DIR" == "$REPO_ROOT" ]]; then
    # Collect package src directories that exist
    for pkg_src in "$REPO_ROOT"/packages/*/src/; do
        [[ -d "$pkg_src" ]] && scan_targets+=("$pkg_src")
    done
    # Add scripts/*.py (not subdirectories — eval/ and tests/ are excluded)
    scan_targets+=("$REPO_ROOT/scripts/")
else
    scan_targets+=("$SCAN_DIR")
fi

# ─── Scan the codebase ────────────────────────────────────────────────
violations=0

for target in "${scan_targets[@]}"; do
    [[ -e "$target" ]] || continue

    matches=$(grep -rnE "$PATTERN" "$target" "${exclude_args[@]}" 2>/dev/null || true)

    [[ -z "$matches" ]] && continue

    while IFS= read -r line; do
        # Check if this match is in an excluded file
        skip=false
        for excl in "${EXCLUDE_FILES[@]}"; do
            if [[ "$line" == *"$excl"* ]]; then
                skip=true
                break
            fi
        done
        "$skip" && continue

        # Check excluded path patterns (tests, eval)
        for pat in "${EXCLUDE_PATH_PATTERNS[@]}"; do
            # shellcheck disable=SC2254
            case "$line" in
                *$pat*) skip=true; break ;;
            esac
        done
        "$skip" && continue

        # Skip comment lines:
        #   Python/shell comments: lines where the match is after a #
        #   SQL comments: lines containing --
        #   Docstring examples: lines containing e.g. or ``
        # Extract the file:lineno:content and check the content portion.
        content="${line#*:}"   # strip filename
        content="${content#*:}"  # strip line number
        # Trim leading whitespace
        trimmed="${content#"${content%%[![:space:]]*}"}"

        # Skip lines that are pure comments
        if [[ "$trimmed" == "#"* ]]; then
            continue
        fi
        # Skip SQL comment lines
        if [[ "$trimmed" == "--"* ]]; then
            continue
        fi
        # Skip docstring-style documentation lines (contain `` or "e.g.")
        if [[ "$content" == *'``'* ]] || [[ "$content" == *'e.g.'* ]]; then
            continue
        fi

        if [[ $violations -eq 0 ]]; then
            echo "ERROR: Found hardcoded Claude model identifier(s) in the codebase."
            echo ""
            echo "  Model identifiers should be centralized in judgemind-config."
            echo "  Import from judgemind_config instead of hardcoding the string:"
            echo ""
            echo "    from judgemind_config import DEFAULT_HAIKU_MODEL"
            echo ""
            echo "  Violations:"
            echo ""
        fi

        echo "    $line"
        violations=$((violations + 1))
    done <<< "$matches"
done

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of hardcoded model identifiers."
    echo ""
    echo "  Fix: Import the model constant from judgemind-config instead."
    echo "  Source of truth: packages/judgemind-config/src/judgemind_config/models.py"
    echo ""
    echo "  If you need a new model constant (e.g. DEFAULT_OPUS_MODEL), add it"
    echo "  to models.py and export it from judgemind-config."
    exit 1
fi

echo "All clean — no hardcoded Claude model identifiers found."
exit 0
