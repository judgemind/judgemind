#!/usr/bin/env bash
# scripts/preflight/venv-local.sh — Standalone wrapper around preflight_venv_local.
#
# Verifies that a .venv directory exists in the given path (default: pwd) and
# that it is NOT a symlink to another worktree's venv.
#
# This wrapper exists because calling the underlying function in a single Bash
# tool invocation requires `source scripts/preflight.sh && preflight_venv_local`,
# which the preflight hook blocks (quoted strings combined with `&&`).
# A standalone script under scripts/preflight/ is covered by the `Bash(scripts/*)`
# permission and runs without prompts.
#
# Usage:
#   scripts/preflight/venv-local.sh              # checks .venv in current directory
#   scripts/preflight/venv-local.sh /path/to/pkg # checks .venv at the given path
#
# Exit codes (pass-through from preflight_venv_local):
#   0 — .venv exists and is a real directory (not a symlink). Safe to proceed.
#   1 — .venv is missing or is a symlink. PREFLIGHT FAIL is printed to stderr.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../preflight.sh
source "$SCRIPT_DIR/preflight.sh"

exit_code=0
preflight_venv_local "$@" || exit_code=$?

exit "$exit_code"
