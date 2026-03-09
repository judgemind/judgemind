#!/usr/bin/env bash
# gemini-review.sh — Run the Gemini cross-model review for the /ralph loop.
#
# Usage:
#   scripts/gemini-review.sh <worktree-path>
#
# This wrapper:
# 1. Prepares the diff and changed-files inputs from the worktree
# 2. Injects the Google API key from Secrets Manager via with-secret.sh
# 3. Runs gemini_review.py with RALPH_STATE_DIR set
#
# Exit codes:
#   0 — Review completed (SHIP or REVISE written to result file)
#   2 — Review skipped gracefully (no API key, API error, etc.)
#   1 — Hard error (missing inputs, bad arguments)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $# -lt 1 ]]; then
    echo "Usage: scripts/gemini-review.sh <worktree-path>" >&2
    exit 1
fi

WORKTREE="$1"
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

# Find a Python interpreter — prefer a worktree venv if available, else system python
PYTHON="python3"
for venv_dir in "${WORKTREE}"/packages/*/.venv/bin/python3; do
    if [[ -x "$venv_dir" ]]; then
        PYTHON="$venv_dir"
        break
    fi
done

# Check if google-genai is available; install from scripts/requirements.txt if not
if ! "$PYTHON" -c "from google import genai" 2>/dev/null; then
    echo "INFO: google-genai not found in current Python. Installing from scripts/requirements.txt..." >&2
    "$PYTHON" -m pip install -r "${SCRIPT_DIR}/requirements.txt" --quiet 2>/dev/null || {
        echo "WARNING: Could not install script dependencies. Skipping Gemini review." >&2
        echo "SKIPPED" > "${STATE_DIR}/gemini-review-result.txt"
        echo "Gemini review skipped: google-genai package not available." > "${STATE_DIR}/gemini-feedback.md"
        exit 2
    }
fi

# Run the review with the Google API key injected from Secrets Manager
export RALPH_STATE_DIR="$STATE_DIR"

"${SCRIPT_DIR}/with-secret.sh" \
    -e GOOGLE_API_KEY=judgemind/google/api-key \
    -- "$PYTHON" "${SCRIPT_DIR}/gemini_review.py"

exit_code=$?

if [[ $exit_code -eq 2 ]]; then
    echo "Gemini review skipped (graceful degradation)." >&2
fi

exit $exit_code
