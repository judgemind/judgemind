#!/usr/bin/env bash
# check-ci-guards-skip-list-coverage.sh — Thin wrapper around the
# Python implementation. See check-ci-guards-skip-list-coverage.py for
# the full behaviour description, exit codes, and Fix-block format.
#
# permanent: true
#
# Usage:
#   scripts/check-ci-guards-skip-list-coverage.sh
#   scripts/check-ci-guards-skip-list-coverage.sh --scripts-dir DIR
#
# Exit codes:
#   0 — every argument-required guard is in SKIP_LIST or marked.
#   1 — one or more argument-required guards are missing.
#   2 — script error.
#
# Tracking: issue #4379 (parent: #4332).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-ci-guards-skip-list-coverage.py" "$@"
