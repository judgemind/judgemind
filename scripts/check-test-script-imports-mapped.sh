#!/usr/bin/env bash
# check-test-script-imports-mapped.sh — wrapper for
# scripts/check_test_script_imports_mapped.py.  Verifies that every
# top-level ``scripts/*.py`` imported by a test under
# ``packages/scraper-framework/tests/`` is in the path filter that
# gates the CI job which runs that test.
#
# Why this guard exists (#4452)
# -----------------------------
# Tests under ``packages/scraper-framework/tests/`` exercise top-level
# scripts like ``scripts/reingest_from_s3.py`` and ``scripts/rebuild_db.py``
# (via ``sys.path`` injection + ``importlib.import_module(...)``).  The
# CI jobs that run those tests gate on ``dorny/paths-filter`` filters
# scoped to ``packages/scraper-framework/**`` — so a PR that modifies
# only the imported ``scripts/*.py`` file skips the test job entirely.
# The next unrelated PR that touches ``packages/scraper-framework/**``
# trips the now-broken tests, blocking every subsequent scraper PR.
#
# This guard structurally enforces the path-filter coverage invariant
# so that bug class stops accruing.  Concrete failure mode the guard
# would have prevented: PR #4421 → #4449 (5 failing tests blocking
# scraper PRs for ~2 weeks until the next unrelated scraper PR
# surfaced the regression).
#
# Issue
# -----
# #4452 (this guard).  Triggering bug: #4449.  Triggering PR: #4421.
#
# Usage
# -----
#   scripts/check-test-script-imports-mapped.sh
#
# Exit codes
# ----------
#   0 — All clean: every test-imported script is covered by the right
#       path filter.
#   1 — At least one violation found.
#   2 — Internal / parse error (helper Python script failed).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$REPO_ROOT/scripts/check_test_script_imports_mapped.py"

if [[ ! -f "$HELPER" ]]; then
    echo "ERROR: helper not found: $HELPER" >&2
    exit 2
fi

# Forward all CLI args.  The helper's defaults already point at the right
# repo paths when invoked from the repo root.
exec python3 "$HELPER" "$@"
