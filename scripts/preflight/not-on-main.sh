#!/usr/bin/env bash
# scripts/preflight/not-on-main.sh — Standalone wrapper around preflight_not_on_main.
#
# Verifies the current branch is not main/master. Prevents accidental commits
# or pushes to the default branch during task work.
#
# This wrapper exists because calling the underlying function in a single Bash
# tool invocation requires `source scripts/preflight.sh && preflight_not_on_main`,
# which the preflight hook blocks (quoted strings combined with `&&`).
# A standalone script under scripts/preflight/ is covered by the `Bash(scripts/*)`
# permission and runs without prompts.
#
# Usage:
#   scripts/preflight/not-on-main.sh
#
# Exit codes (pass-through from preflight_not_on_main):
#   0 — On a non-main/master branch. Safe to proceed.
#   1 — On main or master branch (or detached HEAD). PREFLIGHT FAIL is printed
#       to stderr.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../preflight.sh
source "$SCRIPT_DIR/preflight.sh"

exit_code=0
preflight_not_on_main "$@" || exit_code=$?

exit "$exit_code"
