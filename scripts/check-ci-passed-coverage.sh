#!/usr/bin/env bash
# check-ci-passed-coverage.sh — Thin wrapper for check-ci-passed-coverage.py
#
# Asserts every top-level CI job is listed in ci-passed.needs: so missing
# entries (like the #3919 gap) are caught before merge.
#
# Passes all args through to the Python script — see that file for flags
# and exit codes (#3919).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-ci-passed-coverage.py" "$@"
