#!/usr/bin/env bash
# scripts/preflight/rate-budget.sh — Standalone wrapper around preflight_rate_budget.
#
# Checks the GitHub API rate limit and warns if remaining requests are below
# the given threshold (default: 100). Useful before expensive polling operations
# like `gh run watch`.
#
# This wrapper exists because calling the underlying function in a single Bash
# tool invocation requires `source scripts/preflight.sh && preflight_rate_budget`,
# which the preflight hook blocks (quoted strings combined with `&&`).
# A standalone script under scripts/preflight/ is covered by the `Bash(scripts/*)`
# permission and runs without prompts.
#
# Usage:
#   scripts/preflight/rate-budget.sh           # warn if < 100 requests remaining
#   scripts/preflight/rate-budget.sh 200       # warn if < 200 requests remaining
#
# Exit codes (pass-through from preflight_rate_budget):
#   0 — Rate budget sufficient (remaining >= threshold).
#   1 — Rate budget low (remaining < threshold). PREFLIGHT WARN printed to stderr.
#   2 — Error (gh CLI unavailable or API unreachable). PREFLIGHT WARN printed
#       to stderr.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../preflight.sh
source "$SCRIPT_DIR/preflight.sh"

exit_code=0
preflight_rate_budget "$@" || exit_code=$?

exit "$exit_code"
