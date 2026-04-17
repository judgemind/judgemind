#!/usr/bin/env bash
# check-test-except-pass.sh — Forbid `try/except Exception: pass` in tests.
#
# See scripts/check-test-except-pass.py for the full behaviour description,
# the list of forbidden patterns, and the `# noqa: BLE001` escape hatch.
#
# Usage:
#   scripts/check-test-except-pass.sh           # scans default test dirs
#   scripts/check-test-except-pass.sh DIR ...   # scans specific paths
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more violations found.
#
# Ref: https://github.com/judgemind/judgemind/issues/2459

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/check-test-except-pass.py" "$@"
