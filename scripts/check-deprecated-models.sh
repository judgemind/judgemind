#!/usr/bin/env bash
# check-deprecated-models.sh — Detect deprecated Anthropic model identifiers.
#
# Scans the codebase for known deprecated Anthropic model IDs that will return
# 404 or other errors from the API.  Catches issues at PR time instead of at
# runtime on dev/prod.
#
# Maintains a list of deprecated model identifiers that can be updated as
# Anthropic deprecates older models.
#
# Usage:
#   scripts/check-deprecated-models.sh          # exits 0 if clean, 1 if violations
#   scripts/check-deprecated-models.sh [dir]    # scan a specific directory
#
# Exit codes:
#   0 — No deprecated model identifiers found.
#   1 — One or more files reference deprecated models.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# ─── Deprecated model identifiers ───────────────────────────────────────
# Add new entries as Anthropic deprecates models.  Each entry is a string
# that will be matched literally in source files.
#
# Reference: https://docs.anthropic.com/en/docs/about-claude/models
DEPRECATED_MODELS=(
    # Claude 3 original family (deprecated 2025)
    "claude-3-haiku-20240307"
    "claude-3-sonnet-20240229"
    "claude-3-opus-20240229"

    # Claude 3.5 aliases that resolved to deprecated versions
    "claude-3-5-haiku-latest"
    "claude-3-5-sonnet-latest"
    "claude-3-5-opus-latest"

    # Claude 3.5 dated versions (deprecated in favor of claude-haiku-4-5 / claude-sonnet-4)
    "claude-3-5-haiku-20241022"
    "claude-3-5-sonnet-20240620"
    "claude-3-5-sonnet-20241022"
    "claude-3-5-sonnet-v2"
)

# ─── Directories and files to exclude ────────────────────────────────────
# These contain configuration, documentation, or test fixtures where
# deprecated model names may appear legitimately.
EXCLUDE_DIRS=(
    ".git"
    ".venv"
    "node_modules"
    ".next"
    "__pycache__"
    ".claude"
    ".vite"
)

# Files that legitimately reference deprecated models (this script itself,
# its tests, documentation about the deprecation check).
EXCLUDE_FILES=(
    "scripts/check-deprecated-models.sh"
    "scripts/tests/test_check_deprecated_models.sh"
)

# ─── Build grep arguments ───────────────────────────────────────────────

# Build the alternation pattern: (model1|model2|model3)
# Escape dots for regex since model IDs contain literal dots.
escaped_models=()
for model in "${DEPRECATED_MODELS[@]}"; do
    escaped_models+=("${model//./\\.}")
done
alternation=$(IFS='|'; echo "${escaped_models[*]}")
PATTERN="(${alternation})"

# Build --exclude-dir arguments
exclude_args=()
for dir in "${EXCLUDE_DIRS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Scan the codebase ──────────────────────────────────────────────────

violations=0
violation_files=()

# Use grep -rn with extended regex to find deprecated model references.
# Process matches and filter out excluded files.
matches=$(grep -rnE "$PATTERN" "$SCAN_DIR" "${exclude_args[@]}" 2>/dev/null || true)

if [[ -n "$matches" ]]; then
    while IFS= read -r line; do
        # Check if this match is in an excluded file
        skip=false
        for excl in "${EXCLUDE_FILES[@]}"; do
            if [[ "$line" == *"$excl"* ]]; then
                skip=true
                break
            fi
        done
        if "$skip"; then
            continue
        fi

        if [[ $violations -eq 0 ]]; then
            echo "ERROR: Found deprecated Anthropic model identifier(s) in the codebase."
            echo ""
            echo "  The following files reference models that have been deprecated by"
            echo "  Anthropic and will return 404 or other errors from the API:"
            echo ""
        fi

        echo "    $line"
        violations=$((violations + 1))
    done <<< "$matches"
fi

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of deprecated model identifiers."
    echo ""
    echo "  Fix: Replace deprecated model IDs with current equivalents:"
    echo "    claude-3-haiku-*         -> claude-haiku-4-5-20251001"
    echo "    claude-3-5-haiku-*       -> claude-haiku-4-5-20251001"
    echo "    claude-3-sonnet-*        -> claude-sonnet-4-20250514"
    echo "    claude-3-5-sonnet-*      -> claude-sonnet-4-20250514"
    echo "    claude-3-opus-*          -> claude-opus-4-20250514"
    echo ""
    echo "  To add a new deprecated model, update the DEPRECATED_MODELS array"
    echo "  in scripts/check-deprecated-models.sh."
    echo ""
    echo "  See: https://docs.anthropic.com/en/docs/about-claude/models"
    exit 1
fi

echo "All clean — no deprecated Anthropic model identifiers found."
exit 0
