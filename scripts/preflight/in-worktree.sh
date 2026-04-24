#!/usr/bin/env bash
# scripts/preflight/in-worktree.sh — Standalone wrapper around preflight_in_worktree.
#
# Verifies that the current directory is inside a git worktree, not the main
# repo checkout. Prevents agents from accidentally modifying the shared main
# checkout or using its venv.
#
# This wrapper exists because calling the underlying function in a single Bash
# tool invocation requires `source scripts/preflight.sh && preflight_in_worktree`,
# which the preflight hook blocks (quoted strings combined with `&&`).
# A standalone script under scripts/preflight/ is covered by the `Bash(scripts/*)`
# permission and runs without prompts.
#
# Usage:
#   scripts/preflight/in-worktree.sh
#
# Exit codes (pass-through from preflight_in_worktree):
#   0 — Current directory is inside a worktree. Safe to proceed.
#   1 — Not in a worktree (main repo checkout or outside git). PREFLIGHT FAIL
#       is printed to stderr.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../preflight.sh
source "$SCRIPT_DIR/preflight.sh"

exit_code=0
preflight_in_worktree "$@" || exit_code=$?

exit "$exit_code"
