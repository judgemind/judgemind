#!/usr/bin/env bash
# check-no-basicconfig-with-extra.sh — Forbid ``logging.basicConfig`` +
# ``extra=`` co-occurrence in scripts/*.py without ``configure_structlog``.
#
# Background — the silent extra= field drop
# ─────────────────────────────────────────
# ``logging.basicConfig(format="%(asctime)s %(levelname)-8s %(message)s")``
# is the canonical Python stdlib idiom for one-line console logging.
# The format string looks reasonable, but it silently drops every
# ``extra=`` field passed to ``logger.<level>(...)`` calls because the
# format only references ``%(asctime)s``, ``%(levelname)s``, and
# ``%(message)s`` — nothing that surfaces the LogRecord's extra dict.
# Issue #4368 documented the production incident: a backfill script's
# ``extra=`` fields disappeared from CloudWatch Logs Insights, and the
# post-deploy verification that depended on those fields silently passed.
#
# The fix is ``configure_structlog(json=True, stdlib_bridge=True)``
# from ``packages/scraper-framework/src/framework/logging.py`` — it
# routes stdlib ``logging.getLogger(__name__)`` calls through
# structlog's ProcessorFormatter + ExtraAdder, JSON-encoding the
# LogRecord plus its extras as one event per line.  PR #4368 fixed
# ``scripts/drain_splitter_carry_forward_clusters.py``; #4373 migrated
# the other 13 affected scripts.  This guard prevents the bug from
# re-accruing as ``scripts/*.py`` expands.
#
# What this guard scans
# ─────────────────────
# Every top-level ``scripts/*.py`` file — i.e. files matching the glob
# ``scripts/*.py`` (no recursion).  Subdirectories under ``scripts/``
# (``scripts/archive/``, ``scripts/dispatcher/``, ``scripts/dispatcher_v3/``,
# ``scripts/spotcheck/``, ``scripts/oneoff/``, ``scripts/one_off/``,
# ``scripts/tests/``) are out of scope:
#
#   - ``scripts/archive/`` and ``scripts/oneoff/`` / ``scripts/one_off/``
#     are deprecated one-offs, kept for posterity.
#   - ``scripts/dispatcher/`` and ``scripts/dispatcher_v3/`` are library
#     modules whose entrypoints live in the daemon binary; they
#     compose a logger but do not own the global configuration.
#   - ``scripts/spotcheck/`` and ``scripts/tests/`` are likewise scoped
#     to a different runtime (cron / pytest).
#
# A file is flagged when ALL of the following hold:
#
#   1. The file calls ``logging.basicConfig(...)`` (also via
#      ``from logging import basicConfig`` then ``basicConfig(...)``).
#   2. The file passes ``extra=...`` as a keyword argument to at least
#      one logger method call (``logger.info(...)``, etc.).
#   3. The file does NOT call ``configure_structlog(...)`` anywhere.
#
# Issue #4376.  Parent: #4368.
#
# Usage
# ─────
#
#   scripts/check-no-basicconfig-with-extra.sh        # scan scripts/*.py
#   scripts/check-no-basicconfig-with-extra.sh [path] # scan a specific file or directory
#
# Exit codes
# ──────────
#
#   0 — No violations found.
#   1 — At least one file calls basicConfig + extra= without
#       configure_structlog.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── Determine scan targets ──────────────────────────────────────────
# With no argument: scan top-level scripts/*.py only (the production
# scope per the issue).
# With an argument: scan that path (file or directory) — used by tests.
py_files=()
if [[ $# -eq 0 ]]; then
    while IFS= read -r found; do
        py_files+=("$found")
    done < <(find "$REPO_ROOT/scripts" -maxdepth 1 -type f -name '*.py' | LC_ALL=C sort)
else
    target="$1"
    if [[ -f "$target" && "$target" == *.py ]]; then
        py_files+=("$target")
    elif [[ -d "$target" ]]; then
        # When given a directory, scan only the top level by default.
        # This mirrors the production scope and keeps the test fixture
        # behaviour consistent.
        while IFS= read -r found; do
            py_files+=("$found")
        done < <(find "$target" -maxdepth 1 -type f -name '*.py' | LC_ALL=C sort)
    fi
fi

# Empty file list → no violations possible.
if [[ ${#py_files[@]} -eq 0 ]]; then
    exit 0
fi

# ─── Run the AST scanner ─────────────────────────────────────────────
# The Python scanner emits one line per violation in the form:
#   <path>:<lineno>:<snippet>
python_output="$(python3 "$REPO_ROOT/scripts/check_no_basicconfig_with_extra.py" "${py_files[@]}")"

# ─── Report violations ───────────────────────────────────────────────
if [[ -z "${python_output// /}" ]]; then
    exit 0
fi

violations=0
echo "ERROR: Found Python script(s) calling basicConfig + extra= without configure_structlog."
echo ""
echo "  The combination silently drops every extra= field from CloudWatch"
echo "  output because the basicConfig format string does not reference the"
echo "  LogRecord's extra dict.  See issue #4368 for the production"
echo "  post-deploy verification incident that motivated the migration."
echo ""
echo "  Fix: replace the basicConfig call with configure_structlog from"
echo "  framework.logging.  The canonical pattern is:"
echo ""
echo "      from framework.logging import configure_structlog  # noqa: E402"
echo "      configure_structlog(json=True, stdlib_bridge=True)"
echo "      logger = logging.getLogger(__name__)"
echo ""
echo "  See scripts/drain_splitter_carry_forward_clusters.py for the"
echo "  reference implementation (PR #4368) and #4373 for the bulk"
echo "  migration of the other 13 affected scripts."
echo ""
echo "  Violating file(s):"
echo ""
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    echo "    $entry"
    violations=$((violations + 1))
done <<< "$python_output"

if (( violations > 0 )); then
    echo ""
    echo "  Found $violations file(s) with basicConfig + extra= and no configure_structlog."
    exit 1
fi

exit 0
