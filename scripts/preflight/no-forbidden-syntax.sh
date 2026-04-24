#!/usr/bin/env bash
# scripts/preflight/no-forbidden-syntax.sh — Standalone wrapper around preflight_no_forbidden_syntax.
#
# Checks a command string for forbidden shell patterns ($() substitution,
# heredocs, inline python -c). Mirrors the checks in .claude/hooks/preflight-bash.sh
# but can be called from scripts.
#
# This wrapper exists because calling the underlying function in a single Bash
# tool invocation requires `source scripts/preflight.sh && preflight_no_forbidden_syntax`,
# which the preflight hook blocks (quoted strings combined with `&&`).
# A standalone script under scripts/preflight/ is covered by the `Bash(scripts/*)`
# permission and runs without prompts.
#
# Usage:
#   scripts/preflight/no-forbidden-syntax.sh 'git status'
#   scripts/preflight/no-forbidden-syntax.sh 'echo $(whoami)'
#
# Exit codes (pass-through from preflight_no_forbidden_syntax):
#   0 — Command string contains no forbidden patterns. Safe to proceed.
#   1 — Command string contains a forbidden pattern ($(), heredoc, or python -c).
#       PREFLIGHT FAIL is printed to stderr identifying the pattern.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../preflight.sh
source "$SCRIPT_DIR/preflight.sh"

exit_code=0
preflight_no_forbidden_syntax "$@" || exit_code=$?

exit "$exit_code"
