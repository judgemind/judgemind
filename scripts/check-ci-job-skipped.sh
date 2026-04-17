#!/usr/bin/env bash
# check-ci-job-skipped.sh — Thin wrapper for check-ci-job-skipped.py
#
# Runs the Python checker against the current diff vs origin/main. Used
# by the CI `ci-job-skipped-check` job and by the `.githooks/pre-push`
# hook so the footgun is caught before a push even reaches CI.
#
# Passes all args through to the Python script — see that file for flags
# and exit codes (#2527).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-ci-job-skipped.py" "$@"
