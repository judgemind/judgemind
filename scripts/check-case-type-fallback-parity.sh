#!/usr/bin/env bash
# check-case-type-fallback-parity.sh — Assert the case_type fallback chain
# stays in sync between live ingestion (worker.py) and reingest
# (_apply_regex_fallbacks in reingest_from_s3.py).
#
# Why this guard exists
# ---------------------
# The regex/heuristic fallback chain that fills ``case_type`` when LLM
# extraction returns NULL has historically diverged between
# ``packages/scraper-framework/src/ingestion/worker.py`` (live ingestion path)
# and ``scripts/reingest_from_s3.py`` (reparse path).  Each new
# ``extract_case_type_from_*`` helper added to worker.py must also land in
# ``_apply_regex_fallbacks`` to keep the two paths producing identical
# case_type for the same document.
#
# The pattern has recurred at least six times over four years (#1731, #1749,
# #1763, #1836, #2062 / surfaced as #4263, #2406).  Each gap was a single
# missing call site in one of the two files; each cost multi-hour
# investigations + reingest re-runs on dev.  This check is the cheap
# structural defense — see #4290.
#
# Rule
# ----
# The set of distinct ``extract_case_type_from_<suffix>`` call names in
# ``worker.py`` (excluding the ``from ... import`` statement) MUST equal the
# set in ``_apply_regex_fallbacks`` of ``reingest_from_s3.py``.  Any
# asymmetry — fallback added to one file but not the other — fails the
# check.
#
# Usage
# -----
#   scripts/check-case-type-fallback-parity.sh   # exits 0 if in sync, 1 otherwise
#
# Exit codes
# ----------
#   0 — Both paths reference the same set of case_type fallback helpers.
#   1 — The two sets diverge (details on stderr).
#
# Test override
# -------------
# Set ``CASE_TYPE_PARITY_ROOT`` to a directory containing alternate
# ``packages/scraper-framework/src/ingestion/worker.py`` and
# ``scripts/reingest_from_s3.py`` files.  Used by
# ``scripts/tests/test_check_case_type_fallback_parity.sh`` to drive
# regression coverage without mutating the real source tree.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/check-case-type-fallback-parity.py" "$@"
