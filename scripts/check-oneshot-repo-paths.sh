#!/usr/bin/env bash
# check-oneshot-repo-paths.sh — Detect ECS oneshot scripts that reference repo
# paths without a fallback mechanism.
#
# Scripts run via ecs-run-task.sh are uploaded as single files to S3.  The repo
# filesystem is NOT available inside the container.  If a script resolves
# _REPO_ROOT to locate config files (e.g. data-quality-baselines.json), those
# files will be missing, and the script may silently degrade.
#
# This check scans all Python files in scripts/ for references to REPO_ROOT
# (or _REPO_ROOT) and flags any that are not in the VALIDATED list.  Scripts
# in the validated list have been manually confirmed to have a fallback
# mechanism (e.g. --baselines-base64 CLI arg, .exists() guard, etc.).
#
# Usage:
#   scripts/check-oneshot-repo-paths.sh          # exits 0 if clean, 1 if violations
#   scripts/check-oneshot-repo-paths.sh --dir P  # scan directory P instead of scripts/
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more scripts reference repo paths without a validated fallback.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${SCRIPT_DIR}"

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)
            TARGET_DIR="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

# ─── Local-only scripts (never run as ECS oneshots) ──────────────────────────
# Imported from check-oneshot-imports.sh.  These scripts never run in ECS
# containers, so repo-root references are safe.
LOCAL_ONLY=(
    "gemini_review.py"
    "log_ralph_review.py"
    "log_ralph_summary.py"
    "update-coverage-baselines.py"
    "validate-dq-baselines.py"
    # CI-only checker scripts — run in GitHub Actions, not ECS
    "check-oneshot-imports.sh"
    "check-oneshot-repo-paths.sh"
    "check-duplicate-functions.py"
    "check-sql-conflicts.py"
    "check-deprecated-models.sh"
    "check-hardcoded-models.sh"
    "check-aws-bool-flags.sh"
    "check-hardcoded-colors.sh"
    "check-migration-files.sh"
    # Developer/dispatcher tooling — only run locally
    "dispatcher-request.py"
    "phase_timer.py"
    "screenshot.py"
)

# ─── Validated scripts ────────────────────────────────────────────────────────
# These scripts reference _REPO_ROOT / REPO_ROOT but have been manually
# confirmed to have a proper fallback mechanism for ECS oneshot execution.
#
# To add a script here:
#   1. Confirm the script has a fallback when the repo path is unavailable
#      (e.g. CLI arg like --baselines-base64, or .exists() guard with
#      graceful degradation, or the path is only used for optional features).
#   2. Add the filename and document the fallback mechanism.
VALIDATED=(
    "data-quality-check.py"  # --baselines-base64 / --baselines-json CLI args bypass file path (#1225)
    "check-scraper-zero-record-runner.py"  # Runs in dedicated ECS task (EventBridge, not ecs-run-task.sh); scripts/ dir is baked into the Docker image (#2677)
)

# ─── Helper: is the file in a skip list? ──────────────────────────────────────

is_in_list() {
    local name="$1"
    shift
    for entry in "$@"; do
        if [[ "$name" == "$entry" ]]; then
            return 0
        fi
    done
    return 1
}

# ─── Scan for REPO_ROOT references ───────────────────────────────────────────
# Pattern: variable assignments or references containing REPO_ROOT (with or
# without leading underscore).  We look for the assignment pattern plus any
# usage of the variable to build file paths.

repo_root_pattern='(^|[^a-zA-Z])_?REPO_ROOT'

violations=0

for f in "$TARGET_DIR"/*.py; do
    [[ -f "$f" ]] || continue

    basename_f="$(basename "$f")"

    # Skip local-only scripts
    if is_in_list "$basename_f" "${LOCAL_ONLY[@]}"; then
        continue
    fi

    # Skip already-validated scripts
    if is_in_list "$basename_f" "${VALIDATED[@]}"; then
        continue
    fi

    # Check for REPO_ROOT references (case-sensitive, any form)
    matches=$(grep -nE "$repo_root_pattern" "$f" 2>/dev/null || true)

    if [[ -n "$matches" ]]; then
        echo "ERROR: $basename_f references REPO_ROOT without a validated fallback"
        echo "  ECS oneshot scripts cannot access repo-level files."
        echo "  The repo filesystem is NOT available inside the ECS container."
        echo ""
        echo "  References found:"
        while IFS= read -r line; do
            echo "    $line"
        done <<< "$matches"
        echo ""
        echo "  Fix options:"
        echo "    1. Add a CLI argument to pass the data inline (e.g. --baselines-base64)."
        echo "    2. Add an .exists() guard with graceful fallback when the file is missing."
        echo "    3. If this script is NEVER run as an ECS oneshot, add it to the"
        echo "       LOCAL_ONLY array in scripts/check-oneshot-repo-paths.sh."
        echo "    4. If the fallback already exists, add the script to the VALIDATED"
        echo "       array in scripts/check-oneshot-repo-paths.sh with a comment"
        echo "       documenting the fallback mechanism."
        echo ""
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo "Found $violations script(s) with unvalidated REPO_ROOT references."
    echo "See https://github.com/judgemind/judgemind/issues/1277 for context."
    exit 1
fi

echo "All scripts clean — no unvalidated REPO_ROOT references detected."
exit 0
