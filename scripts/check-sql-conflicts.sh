#!/usr/bin/env bash
# check-sql-conflicts.sh — Validate ON CONFLICT targets in Python SQL scripts.
#
# Wrapper around check-sql-conflicts.py that runs with the system Python.
# The Python script scans scripts/*.py and packages/**/*.py for ON CONFLICT
# clauses and validates them against the known schema constraints.
#
# Usage:
#   scripts/check-sql-conflicts.sh          # exits 0 if clean, 1 if violations
#   scripts/check-sql-conflicts.sh --verbose # show every ON CONFLICT found
#
# Exit codes:
#   0 — No invalid ON CONFLICT targets found.
#   1 — One or more invalid ON CONFLICT targets detected.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/check-sql-conflicts.py" "$@"
