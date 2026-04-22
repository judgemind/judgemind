#!/usr/bin/env bash
# check-duplicate-pr.sh — Standalone wrapper around preflight_no_duplicate_pr.
#
# Checks whether an open PR already exists for the given issue number, so a
# /task agent can adopt the existing PR instead of creating a duplicate.
#
# This exists because calling the underlying function in a single Bash tool
# invocation requires `source scripts/preflight.sh && preflight_no_duplicate_pr
# <N>`, which the preflight hook blocks (quoted strings combined with `&&`).
# A standalone script under scripts/ is covered by the `Bash(scripts/*)`
# permission and runs without prompts. See #2706.
#
# Usage:
#   scripts/check-duplicate-pr.sh <issue_number>
#   scripts/check-duplicate-pr.sh 42
#   scripts/check-duplicate-pr.sh '#42'       # leading # is stripped
#
# Exit codes (pass-through from preflight_no_duplicate_pr):
#   0 — Duplicate PR found. A "duplicate:" line is printed to stdout with the
#       existing PR number. Caller should adopt that PR instead of creating a
#       new one.
#   1 — No duplicate PR. An "ok:" line is printed to stdout. Safe to create a
#       new one.
#   2 — Error (missing argument, gh CLI unavailable, or API failure). An
#       "error:" line is printed to stderr.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# Strip a leading '#' from the issue argument (if present) for display.
issue_arg="${1:-}"
issue_num="${issue_arg#\#}"

# Capture stdout (bare PR number on duplicate, empty otherwise) while letting
# stderr (PREFLIGHT WARN/FAIL lines) flow through to the caller's terminal.
exit_code=0
pr_number=$(preflight_no_duplicate_pr "$@") || exit_code=$?

if [[ -z "$issue_num" ]]; then
    # Missing argument — the function already printed a PREFLIGHT FAIL line to
    # stderr. Just propagate exit 2 without adding a redundant message.
    exit "$exit_code"
fi

case "$exit_code" in
    0)
        echo "duplicate: open PR #${pr_number} already addresses #${issue_num} (exit 0 — adopt it)"
        ;;
    1)
        echo "ok: no open PR found for #${issue_num} — safe to create PR (exit 1)"
        ;;
    *)
        echo "error: check-duplicate-pr.sh failed for #${issue_num} (exit 2)" >&2
        ;;
esac

exit "$exit_code"
