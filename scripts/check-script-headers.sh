#!/usr/bin/env bash
# check-script-headers.sh — Enforce the one-off/permanent marker convention.
#
# See scripts/check-script-headers.py for the full behaviour description,
# the list of matched name patterns, the marker window, and the exemption
# rules.
#
# Usage:
#   scripts/check-script-headers.sh            # scan scripts/ (default)
#   scripts/check-script-headers.sh PATH ...   # scan specific paths
#
# Exit codes:
#   0 — All matched scripts carry a marker.
#   1 — One or more matched scripts are missing a marker.
#
# Ref: https://github.com/judgemind/judgemind/issues/2533

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/check-script-headers.py" "$@"
