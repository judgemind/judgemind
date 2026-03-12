#!/usr/bin/env bash
# check-oneshot-imports.sh — Detect scripts/ Python files that import sibling modules.
#
# ECS oneshot scripts (run via ecs-run-task.sh) are uploaded as single files
# to S3.  They cannot import other local .py files from scripts/ because those
# files are not present in the container.  Only stdlib, installed packages, and
# _venv_helper (which has a stub created in-container) are available.
#
# This script scans all Python files in scripts/ and flags any that contain
# top-level imports of sibling .py modules.  Files listed in the LOCAL_ONLY
# array are excluded — they are known to be run only locally (never as ECS
# oneshots) and may legitimately import siblings.
#
# Usage:
#   scripts/check-oneshot-imports.sh          # exits 0 if clean, 1 if violations found
#   scripts/check-oneshot-imports.sh --list   # list all sibling module names
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more scripts import sibling modules.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Local-only scripts (never run as ECS oneshots) ────────────────────────
# These scripts are only run on developer machines or CI runners where the
# full scripts/ directory is available.  They may import sibling modules.
#
# To add a script here, append its filename (basename only) and add a comment
# explaining why it is local-only.
LOCAL_ONLY=(
    "gemini_review.py"          # Ralph loop reviewer — runs locally in worktree
    "log_ralph_review.py"       # Ralph loop logger — runs locally in worktree
    "log_ralph_summary.py"      # Ralph loop summary — runs locally in worktree
    "update-coverage-baselines.py"  # CI-only — runs in GitHub Actions runner
    "validate-dq-baselines.py"      # CI-only — validates baselines JSON structure
)

# ─── Discover sibling module names ─────────────────────────────────────────
# Every .py file in scripts/ (except __init__.py, _venv_helper.py, and test
# files) is a potential sibling module that could be mistakenly imported.

sibling_modules=()
for f in "$SCRIPT_DIR"/*.py; do
    basename_f="$(basename "$f")"
    case "$basename_f" in
        __init__.py|_venv_helper.py)
            continue
            ;;
    esac
    # Module name = filename without .py extension, with hyphens replaced by
    # underscores (Python import convention).
    module_name="${basename_f%.py}"
    module_name="${module_name//-/_}"
    sibling_modules+=("$module_name")
done

if [[ "${1:-}" == "--list" ]]; then
    printf '%s\n' "${sibling_modules[@]}"
    exit 0
fi

# ─── Build the is-local-only lookup ────────────────────────────────────────

is_local_only() {
    local name="$1"
    for lo in "${LOCAL_ONLY[@]}"; do
        if [[ "$name" == "$lo" ]]; then
            return 0
        fi
    done
    return 1
}

# ─── Build a grep pattern matching top-level imports of sibling modules ────
# Matches only module-level (unindented) imports:
#   from <module> import ...
#   import <module>
#   import <module> as ...
#   import <module>.<sub>
# Does NOT match:
#   # from <module> import ...            (commented out)
#       import <module>                   (indented — inside a function/try block)
#
# Indented imports are allowed because they are conditional (lazy imports
# inside functions or try/except blocks).  This is the accepted pattern for
# optional dependencies in oneshot scripts — see data-quality-check.py.

# Build alternation: (module1|module2|module3)
alternation=$(IFS='|'; echo "${sibling_modules[*]}")
import_pattern="^(from[[:space:]]+(${alternation})[[:space:]]+import|import[[:space:]]+(${alternation})([[:space:]]|$|\\.))"

# ─── Scan each script ─────────────────────────────────────────────────────

violations=0

for f in "$SCRIPT_DIR"/*.py; do
    basename_f="$(basename "$f")"

    # Skip known local-only scripts
    if is_local_only "$basename_f"; then
        continue
    fi

    # Skip non-script files (modules that are only imported, never executed)
    # Heuristic: files without a shebang or __main__ guard are likely libraries.
    # But we still check them — if they import siblings and someone runs them
    # as a oneshot, it will break.

    # Use grep with extended regex to find sibling imports
    matches=$(grep -nE "$import_pattern" "$f" 2>/dev/null || true)

    if [[ -n "$matches" ]]; then
        echo "ERROR: $basename_f imports sibling module(s) from scripts/"
        echo "  ECS oneshot scripts must be self-contained (single-file)."
        echo "  Sibling .py files are NOT available in the ECS container."
        echo ""
        echo "  Violations:"
        while IFS= read -r line; do
            echo "    $line"
        done <<< "$matches"
        echo ""
        echo "  Fix options:"
        echo "    1. Inline the needed code or use a lazy import guarded by try/except."
        echo "    2. Move shared code into an installed package (e.g. scraper-framework)."
        echo "    3. If this script is NEVER run as an ECS oneshot, add it to the"
        echo "       LOCAL_ONLY array in scripts/check-oneshot-imports.sh."
        echo ""
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo "Found $violations script(s) with sibling imports."
    echo "See https://github.com/judgemind/judgemind/issues/835 for context."
    exit 1
fi

echo "All scripts clean — no sibling imports detected."
exit 0
