#!/usr/bin/env bash
# check-workflow-paths-filter-coverage.sh — Thin wrapper for
# check-workflow-paths-filter-coverage.py.
#
# Verifies that every shell script invoked from the runner inside a
# GitHub Actions workflow (or a composite action it references) is
# present in that workflow's `paths:` filter, so changes to the shared
# script trigger the workflow on the PR that introduces them.
#
# Passes all args through to the Python script — see that file for
# flags and exit codes (#4084).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-workflow-paths-filter-coverage.py" "$@"
