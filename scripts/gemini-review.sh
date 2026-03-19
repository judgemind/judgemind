#!/usr/bin/env bash
# gemini-review.sh — Run the Gemini cross-model review for the /ralph loop.
#
# Usage:
#   scripts/gemini-review.sh <worktree-path> [--adversarial]
#
# This wrapper:
# 1. Prepares the diff and changed-files inputs from the worktree
# 2. Injects the Google API key from Secrets Manager via with-secret.sh
# 3. Runs gemini_review.py with RALPH_STATE_DIR set
#
# Options:
#   --adversarial   Run in adversarial (bug-hunting) mode instead of standard review.
#                   Sets GEMINI_REVIEW_MODE=adversarial and writes to adversarial-result.txt
#                   and adversarial-feedback.md instead of the standard output files.
#
# Exit codes:
#   0 — Review completed (SHIP or REVISE written to result file)
#   2 — Review skipped gracefully (no API key, API error, etc.)
#   1 — Hard error (missing inputs, bad arguments)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse arguments
ADVERSARIAL=false
WORKTREE=""

for arg in "$@"; do
    case "$arg" in
        --adversarial)
            ADVERSARIAL=true
            ;;
        *)
            if [[ -z "$WORKTREE" ]]; then
                WORKTREE="$arg"
            else
                echo "ERROR: Unexpected argument: $arg" >&2
                echo "Usage: scripts/gemini-review.sh <worktree-path> [--adversarial]" >&2
                exit 1
            fi
            ;;
    esac
done

if [[ -z "$WORKTREE" ]]; then
    echo "Usage: scripts/gemini-review.sh <worktree-path> [--adversarial]" >&2
    exit 1
fi

STATE_DIR="${WORKTREE}/tmp/ralph"

if [[ ! -d "$STATE_DIR" ]]; then
    echo "ERROR: Ralph state directory not found: ${STATE_DIR}" >&2
    exit 1
fi

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

# Ensure a Python environment with google-genai is available.
#
# The Python script uses _venv_helper to re-launch itself inside the
# scraper-framework venv.  But for non-scraper tasks (API, frontend, infra),
# that venv may not exist.  In that case, create a lightweight .venv-scripts
# venv with just the script dependencies and tell _venv_helper to skip its
# venv check so the Python script runs directly in the scripts venv.
SCRAPER_VENV="${WORKTREE}/packages/scraper-framework/.venv/bin/python3"
PYTHON="python3"

if [[ -x "$SCRAPER_VENV" ]]; then
    # Scraper-framework venv exists — _venv_helper will handle re-launch
    :
else
    # No scraper-framework venv — create/reuse a lightweight scripts venv
    SCRIPTS_VENV="${WORKTREE}/.venv-scripts"
    if [[ ! -d "$SCRIPTS_VENV" ]]; then
        echo "INFO: Creating scripts venv at ${SCRIPTS_VENV}..." >&2
        BASE_PYTHON=""
        for candidate in python3.12 python3.11 python3; do
            if command -v "$candidate" &>/dev/null; then
                BASE_PYTHON="$candidate"
                break
            fi
        done
        BASE_PYTHON="${BASE_PYTHON:-python3}"

        "$BASE_PYTHON" -m venv "$SCRIPTS_VENV" 2>/dev/null || {
            echo "WARNING: Could not create scripts venv. Skipping Gemini review." >&2
            echo "SKIPPED" > "${STATE_DIR}/gemini-review-result.txt"
            echo "Gemini review skipped: could not create scripts venv." > "${STATE_DIR}/gemini-feedback.md"
            exit 2
        }
    fi

    PYTHON="${SCRIPTS_VENV}/bin/python3"

    # Install dependencies if google-genai is not yet available
    if ! "$PYTHON" -c "from google import genai" 2>/dev/null; then
        echo "INFO: Installing script dependencies into ${SCRIPTS_VENV}..." >&2
        "$PYTHON" -m pip install -r "${SCRIPT_DIR}/requirements.txt" --quiet 2>/dev/null || {
            echo "WARNING: Could not install script dependencies. Skipping Gemini review." >&2
            echo "SKIPPED" > "${STATE_DIR}/gemini-review-result.txt"
            echo "Gemini review skipped: google-genai package not available." > "${STATE_DIR}/gemini-feedback.md"
            exit 2
        }
    fi

    # Tell _venv_helper to skip the scraper-framework venv check since
    # we are running in the scripts venv which has all needed deps.
    export _VENV_HELPER_SKIP=1
fi

# Run the review with the Google API key injected from Secrets Manager
export RALPH_STATE_DIR="$STATE_DIR"

if [[ "$ADVERSARIAL" == "true" ]]; then
    export GEMINI_REVIEW_MODE="adversarial"
    echo "Running Gemini adversarial review..." >&2
else
    export GEMINI_REVIEW_MODE="standard"
    echo "Running Gemini standard review..." >&2
fi

"${SCRIPT_DIR}/with-secret.sh" \
    -e GOOGLE_API_KEY=judgemind/google/api-key \
    -- "$PYTHON" "${SCRIPT_DIR}/gemini_review.py"

exit_code=$?

if [[ $exit_code -eq 2 ]]; then
    echo "Gemini ${GEMINI_REVIEW_MODE} review skipped (graceful degradation)." >&2
fi

exit $exit_code
