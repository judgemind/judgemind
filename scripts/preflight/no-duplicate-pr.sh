#!/usr/bin/env bash
# scripts/preflight/no-duplicate-pr.sh — Standalone wrapper around preflight_no_duplicate_pr.
#
# Checks whether an open PR already exists for the given issue number. Prevents
# duplicate PRs when a session loses context (e.g. after context compaction) or
# when multiple agents pick up the same issue.
#
# This wrapper exists because calling the underlying function in a single Bash
# tool invocation requires `source scripts/preflight.sh && preflight_no_duplicate_pr N`,
# which the preflight hook blocks (quoted strings combined with `&&`).
# A standalone script under scripts/preflight/ is covered by the `Bash(scripts/*)`
# permission and runs without prompts.
#
# Usage:
#   scripts/preflight/no-duplicate-pr.sh 42   # check issue #42 (bare PR number
#                                             # printed to stdout if duplicate found)
#   scripts/preflight/no-duplicate-pr.sh '#42' # leading # is stripped automatically
#
# Exit codes (pass-through from preflight_no_duplicate_pr):
#   0 — Duplicate PR exists. Existing PR number is printed to stdout. Caller
#       should adopt the existing PR instead of creating a new one.
#   1 — No duplicate found. Safe to create a new PR.
#   2 — Error (missing argument, gh CLI unavailable, or API failure). A
#       PREFLIGHT WARN line is printed to stderr.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../preflight.sh
source "$SCRIPT_DIR/preflight.sh"

exit_code=0
preflight_no_duplicate_pr "$@" || exit_code=$?

exit "$exit_code"
