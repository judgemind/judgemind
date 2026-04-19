#!/usr/bin/env bash
# check-dispatcher-image-deps.sh — Thin wrapper for check-dispatcher-image-deps.py
#
# Verifies that every non-stdlib import in scripts/dispatcher/**/*.py has a
# matching `pip install` entry in Dockerfile.dispatcher. The full parser
# (AST walk for imports, Dockerfile RUN scan, stdlib filter) lives in the
# Python companion file — this wrapper exists so CI and the pre-push hook
# can invoke the check without spelling `python3 scripts/check-...` every
# time.
#
# Passes all args through to the Python script — see that file for flags
# and exit codes (#2776).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-dispatcher-image-deps.py" "$@"
