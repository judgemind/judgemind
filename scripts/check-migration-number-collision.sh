#!/usr/bin/env bash
# check-migration-number-collision.sh — Thin wrapper for
# check-migration-number-collision.py.
#
# Runs the Python checker against the current diff vs origin/main, using
# `gh pr list --state open --json number,title,files` to enumerate other
# open PRs. Used by the CI `migration-collision-check` job and
# manually reproducible locally.
#
# Passes all args through to the Python script — see that file for flags
# and exit codes (#2916).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-migration-number-collision.py" "$@"
