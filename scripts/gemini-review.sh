#!/usr/bin/env bash
# gemini-review.sh — Run the Gemini cross-model review for the /ralph loop.
#
# Usage:
#   scripts/gemini-review.sh <worktree-path> [--adversarial] [--plan]
#
# This wrapper:
# 1. Prepares the diff and changed-files inputs from the worktree (code review)
#    or reads plan.md (plan review)
# 2. Injects the Google API key from Secrets Manager via with-secret.sh
# 3. Runs gemini_review.py with RALPH_STATE_DIR set
#
# Options:
#   --adversarial   Run in adversarial (bug-hunting) mode instead of standard review.
#                   Sets GEMINI_REVIEW_MODE=adversarial and writes to adversarial-result.txt
#                   and adversarial-feedback.md instead of the standard output files.
#   --plan          Review an implementation plan instead of a code diff.
#                   Sets GEMINI_REVIEW_PHASE=plan. Reads plan.md and task.md from the
#                   ralph state directory. Writes to plan-gemini-result.txt /
#                   plan-adversarial-result.txt and corresponding feedback files.
#
# Exit codes:
#   0 — Review completed (SHIP/APPROVE or REVISE written to result file)
#   2 — Review skipped gracefully (no API key, API error, plan.md missing, etc.)
#   1 — Hard error (missing inputs, bad arguments)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse arguments
ADVERSARIAL=false
PLAN=false
WORKTREE=""

for arg in "$@"; do
    case "$arg" in
        --adversarial)
            ADVERSARIAL=true
            ;;
        --plan)
            PLAN=true
            ;;
        *)
            if [[ -z "$WORKTREE" ]]; then
                WORKTREE="$arg"
            else
                echo "ERROR: Unexpected argument: $arg" >&2
                echo "Usage: scripts/gemini-review.sh <worktree-path> [--adversarial] [--plan]" >&2
                exit 1
            fi
            ;;
    esac
done

if [[ -z "$WORKTREE" ]]; then
    echo "Usage: scripts/gemini-review.sh <worktree-path> [--adversarial] [--plan]" >&2
    exit 1
fi

STATE_DIR="${WORKTREE}/tmp/ralph"

if [[ ! -d "$STATE_DIR" ]]; then
    echo "ERROR: Ralph state directory not found: ${STATE_DIR}" >&2
    exit 1
fi

# Generate diff and changed files (skip for plan reviews)
if [[ "$PLAN" == "false" ]]; then
    # Generate diff.txt from the worktree
    git -C "$WORKTREE" diff > "${STATE_DIR}/diff.txt" 2>/dev/null || true
    git -C "$WORKTREE" diff --cached >> "${STATE_DIR}/diff.txt" 2>/dev/null || true

    # Generate changed_files.txt — full content of files that have changes
    changed=$(git -C "$WORKTREE" diff --name-only HEAD 2>/dev/null || true)
    cached=$(git -C "$WORKTREE" diff --cached --name-only 2>/dev/null || true)
    untracked=$(git -C "$WORKTREE" ls-files --others --exclude-standard 2>/dev/null || true)

    # Combine and deduplicate
    all_files=$(echo -e "${changed}\n${cached}\n${untracked}" | sort -u | grep -v '^$' || true)

    # Write full content of each changed file
    > "${STATE_DIR}/changed_files.txt"
    for f in $all_files; do
        filepath="${WORKTREE}/${f}"
        if [[ -f "$filepath" ]]; then
            echo "=== ${f} ===" >> "${STATE_DIR}/changed_files.txt"
            cat "$filepath" >> "${STATE_DIR}/changed_files.txt"
            echo "" >> "${STATE_DIR}/changed_files.txt"
        fi
    done
fi

# Use a dedicated scripts venv for script dependencies (google-genai, anthropic).
# This avoids depending on whichever package venv happens to exist first
# (which may not include google-genai) and avoids PEP 668 issues with system Python.
SCRIPTS_VENV="${WORKTREE}/tmp/.scripts-venv"
PYTHON="${SCRIPTS_VENV}/bin/python3"

if [[ ! -x "$PYTHON" ]]; then
    echo "INFO: Creating scripts venv at ${SCRIPTS_VENV}..." >&2

    # Find a base Python to create the venv — prefer python3.12, then 3.11, then python3
    BASE_PYTHON=""
    for candidate in python3.12 python3.11 python3; do
        if command -v "$candidate" &>/dev/null; then
            BASE_PYTHON="$candidate"
            break
        fi
    done
    BASE_PYTHON="${BASE_PYTHON:-python3}"

    "$BASE_PYTHON" -m venv "$SCRIPTS_VENV" || {
        echo "WARNING: Could not create scripts venv. Skipping Gemini review." >&2
        echo "SKIPPED" > "${STATE_DIR}/gemini-review-result.txt"
        echo "Gemini review skipped: failed to create scripts venv." > "${STATE_DIR}/gemini-feedback.md"
        exit 2
    }

    echo "INFO: Installing script dependencies from scripts/requirements.txt..." >&2
    "${SCRIPTS_VENV}/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt" --quiet || {
        echo "WARNING: Could not install script dependencies. Skipping Gemini review." >&2
        echo "SKIPPED" > "${STATE_DIR}/gemini-review-result.txt"
        echo "Gemini review skipped: pip install failed. Check stderr for details." > "${STATE_DIR}/gemini-feedback.md"
        exit 2
    }
fi

# Run the review with the Google API key injected from Secrets Manager
export RALPH_STATE_DIR="$STATE_DIR"

if [[ "$ADVERSARIAL" == "true" ]]; then
    export GEMINI_REVIEW_MODE="adversarial"
else
    export GEMINI_REVIEW_MODE="standard"
fi

if [[ "$PLAN" == "true" ]]; then
    export GEMINI_REVIEW_PHASE="plan"
    echo "Running Gemini ${GEMINI_REVIEW_MODE} plan review..." >&2
else
    export GEMINI_REVIEW_PHASE="code"
    echo "Running Gemini ${GEMINI_REVIEW_MODE} review..." >&2
fi

"${SCRIPT_DIR}/with-secret.sh" \
    -e GOOGLE_API_KEY=judgemind/google/api-key \
    -- "$PYTHON" "${SCRIPT_DIR}/gemini_review.py"

exit_code=$?

if [[ $exit_code -eq 2 ]]; then
    echo "Gemini ${GEMINI_REVIEW_MODE} ${GEMINI_REVIEW_PHASE} review skipped (graceful degradation)." >&2
fi

exit $exit_code
