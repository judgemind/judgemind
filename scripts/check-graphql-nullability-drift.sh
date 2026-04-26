#!/usr/bin/env bash
# check-graphql-nullability-drift.sh — Thin wrapper for
# check-graphql-nullability-drift.py.
#
# Scans migration files in the current diff for DROP NOT NULL statements,
# then checks whether any mapped GraphQL fields are still declared non-null.
# Used by the CI `graphql-nullability-drift-check` job and reproducible locally.
#
# Passes all args through to the Python script — see that file for flags
# and exit codes (#3441).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-graphql-nullability-drift.py" "$@"
