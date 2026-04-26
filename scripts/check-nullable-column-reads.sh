#!/usr/bin/env bash
# check-nullable-column-reads.sh — Thin wrapper for
# check-nullable-column-reads.py.
#
# Scans migration files in the current diff for DROP NOT NULL statements,
# then audits source directories for unguarded reads of those columns.
# Used by the CI `nullable-column-reads-check` job and reproducible locally.
#
# Passes all args through to the Python script — see that file for flags
# and exit codes (#3396).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-nullable-column-reads.py" "$@"
