#!/usr/bin/env bash
# check-duplicate-functions.sh — Detect duplicate top-level function/class
# definitions in Python source files.
#
# ruff's F811 rule does NOT catch duplicate function names at module scope.
# This script fills that gap by parsing Python files with the ast module.
#
# Usage:
#   scripts/check-duplicate-functions.sh          # exits 0 if clean, 1 if violations
#   scripts/check-duplicate-functions.sh DIR ...   # scan specific directories
#
# Exit codes:
#   0 — No duplicates found.
#   1 — One or more duplicate definitions found.
#
# Ref: https://github.com/judgemind/judgemind/issues/1300

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/check-duplicate-functions.py" "$@"
