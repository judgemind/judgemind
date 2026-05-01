#!/usr/bin/env bash
# check-per-phase-timeouts.sh — Thin wrapper for check-per-phase-timeouts.py
#
# Verifies that every *_TIMEOUT_SECONDS constant referenced inside a
# phase-loop function (a function that dispatches on a ``phase`` argument)
# uses a per-phase access form or carries a ``# global-by-design (#NNNN)``
# annotation. Prevents the class of bug where a new global timeout cap is
# added and accidentally applied uniformly across all phases instead of
# being tuned per-phase.
#
# Exit codes: 0 = clean, 1 = violations found, 2 = CLI/IO error.
# Tracking issue: #3776.
#
# Passes all args through to the Python script so the test fixture can
# supply a custom target directory.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-per-phase-timeouts.py" "$@"
