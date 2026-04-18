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
#   0 — Duplicate PR found. The existing PR number is printed to stdout.
#       Caller should adopt that PR instead of creating a new one.
#   1 — No duplicate PR. Safe to create a new one.
#   2 — Error (missing argument, gh CLI unavailable, or API failure).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

preflight_no_duplicate_pr "$@"
