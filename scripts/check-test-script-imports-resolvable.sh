#!/usr/bin/env bash
# check-test-script-imports-resolvable.sh — wrapper for
# scripts/check_test_script_imports_resolvable.py.  Flags tests under
# ``packages/scraper-framework/tests/`` that import a ``scripts/<name>.py``
# module which has been archived (``scripts/archive/<name>.py`` or
# ``scripts/oneoff/<name>.py``) or is otherwise unresolvable.
#
# Why this guard exists (#4464, sibling of #4452)
# -----------------------------------------------
# The mapped-imports guard (#4452) intentionally ignores imports of
# non-existent / archived scripts because the path-filter mapping
# invariant only applies to live ``scripts/*.py``. That left a gap:
# tests can sit in the tree forever importing scripts that have been
# moved to ``scripts/archive/`` (or never existed), failing collection
# when something runs them. Issue #4459 drained the back-catalog of
# ~22 such tests by hand. This guard prevents the next one from
# silently re-accruing.
#
# Patterns covered (AST-based — same coverage as the mapped guard
# plus ``importlib.util.spec_from_file_location`` literal-path)
# ------------------------------------------------------------
#     import <name>
#     from <name> import ...
#     importlib.import_module("<name>")
#     importlib.util.spec_from_file_location("<name>", <path-arg>)
#
# Issue
# -----
# #4464 (this guard).  Sibling of #4452 (mapped-imports guard).  Drains
# the back-catalog tracked by #4459.
#
# Usage
# -----
#   scripts/check-test-script-imports-resolvable.sh
#
# Exit codes
# ----------
#   0 — All clean: no test imports an archived/unresolvable script.
#   1 — At least one violation found.
#   2 — Internal / parse error (helper Python script failed).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$REPO_ROOT/scripts/check_test_script_imports_resolvable.py"

if [[ ! -f "$HELPER" ]]; then
    echo "ERROR: helper not found: $HELPER" >&2
    exit 2
fi

# Forward all CLI args.  The helper's defaults already point at the right
# repo paths when invoked from the repo root.
exec python3 "$HELPER" "$@"
