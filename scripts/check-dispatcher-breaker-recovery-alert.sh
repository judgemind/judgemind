#!/usr/bin/env bash
# check-dispatcher-breaker-recovery-alert.sh — Thin wrapper around the
# Python implementation. See check-dispatcher-breaker-recovery-alert.py
# for the full behaviour description, exit codes, and Fix-block format.
#
# permanent: true
#
# Usage:
#   scripts/check-dispatcher-breaker-recovery-alert.sh
#   scripts/check-dispatcher-breaker-recovery-alert.sh --list
#   scripts/check-dispatcher-breaker-recovery-alert.sh --daemon-path PATH
#
# Exit codes:
#   0 — every breaker trip tag is registered AND every registry entry's
#       recovery + alert methods exist; OR --list was passed.
#   1 — one or more invariants violated (missing recovery/alert, or an
#       unregistered kill-switch trip tag).
#   2 — script error (daemon.py missing).
#
# Tracking: issue #4593 (root cause: #4586, prior fix: #3779).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-dispatcher-breaker-recovery-alert.py" "$@"
