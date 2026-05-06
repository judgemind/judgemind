#!/usr/bin/env bash
# check-tests-use-reingest-helper.sh — Hygiene check enforcing the use of
# the shared ``make_reingest_cap_doc`` helper for reingest-shape
# regression tests under ``packages/scraper-framework/tests/courts/``.
#
# Why this check exists
# ---------------------
# Issue #4153 introduced
# ``packages/scraper-framework/tests/helpers/reingest.py`` exporting the
# shared ``make_reingest_cap_doc(...)`` helper.  Three regression tests
# now use it:
#
#   * tests/courts/test_courtlistener.py            (#3986, migrated in #4153)
#   * tests/courts/ca/test_cc_tentatives_portal.py  (#4133, migrated in #4153)
#   * tests/courts/test_sf_civil_tentatives.py      (#4134, migrated in #4165)
#
# #4134 had to be re-migrated in #4165 because it landed *before* #4153
# and built its own inline ``CapturedDocument(...)`` constructions.
# Without this CI guard, the next scraper that needs a reingest-path
# regression test could repeat the pattern — a fresh inline cap_doc
# construction that drifts from the helper's contract.  This guard is
# the test-side analog of #4141's production-side guard.
#
# What is a reingest-shape CapturedDocument call?
# -----------------------------------------------
# A ``CapturedDocument(...)`` call is reingest-shape when it passes a
# superset of the identifier-fields set:
#
#   {document_id, scraper_id, state, county, court, source_url,
#    capture_timestamp, content_format, raw_content, content_hash}
#
# AND does NOT pass any of:
#
#   {case_number, case_title, judge_name, hearing_date, ruling_text,
#    ruling_text_html, outcome, motion_type, parties, extra,
#    courthouse, department}
#
# These two conditions together define exactly the shape that
# ``scripts/reingest_from_s3.py::_reparse_document`` produces.  Every
# such cap_doc construction in tests/courts/ MUST go through
# ``make_reingest_cap_doc(...)`` so the helper's contract is the
# single source of truth.
#
# Issue
# -----
# #4190 (this check).  Helper: #4153.  Production analog: #4141.
# Audit: #4046.  Failure-shape origin: #3986.
#
# Usage
# -----
#   scripts/check-tests-use-reingest-helper.sh        # scan default tests/courts/
#   scripts/check-tests-use-reingest-helper.sh PATH   # scan a specific dir
#                                                     # (used by tests)
#
# Exit codes
# ----------
#   0 — No inline reingest-shape CapturedDocument(...) constructions found.
#   1 — At least one violation found.
#   2 — Internal error (Python scanner failed to run).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCANNER="$REPO_ROOT/scripts/check_tests_use_reingest_helper.py"

# ─── Resolve scan root ────────────────────────────────────────────────────
# With no argument: scan the production tests/courts/ tree (the default).
# With an argument: scan that path (used by tests for fixture isolation).
if [[ $# -eq 0 ]]; then
    SCAN_ARGS=()
else
    SCAN_ARGS=(--root "$1")
fi

# ─── Run the scanner ──────────────────────────────────────────────────────
# The scanner emits one line per violation in the form:
#   <path>:<lineno>:CapturedDocument(...)
#
# It always exits 0; an empty stdout means "no violations".  We capture
# stdout and decide the wrapper's exit code from there — same split as
# check-parse-document-reingest-safety.sh / check_parse_document_reingest_safety.py.
if ! python_output="$(python3 "$SCANNER" ${SCAN_ARGS[@]+"${SCAN_ARGS[@]}"})"; then
    echo "ERROR: scanner failed to run (see stderr above)." >&2
    exit 2
fi

# ─── Report violations ────────────────────────────────────────────────────
if [[ -z "${python_output// /}" ]]; then
    exit 0
fi

violations=0
echo "ERROR: Inline reingest-shape CapturedDocument(...) construction(s) in tests/courts/."
echo ""
echo "  A reingest-shape CapturedDocument call passes only the identifier"
echo "  fields (document_id, scraper_id, state, county, court, source_url,"
echo "  capture_timestamp, content_format, raw_content, content_hash) and"
echo "  none of the parsed fields (case_number, case_title, judge_name,"
echo "  hearing_date, ruling_text, ruling_text_html, outcome, motion_type,"
echo "  parties, extra, courthouse, department).  Reingest regression"
echo "  tests MUST use the shared helper instead:"
echo ""
echo "    from helpers.reingest import make_reingest_cap_doc"
echo ""
echo "    cap_doc = make_reingest_cap_doc("
echo "        raw_content=raw,"
echo "        scraper_id=\"<scraper-id>\","
echo "        ...,"
echo "    )"
echo ""
echo "  The helper centralizes the contract — the same cap_doc shape that"
echo "  scripts/reingest_from_s3.py::_reparse_document produces — so every"
echo "  test exercising the reingest path stays aligned.  See #4153 for"
echo "  the helper, #4141 for the production-side guard, and #4046 for"
echo "  the audit."
echo ""
echo "  Existing reingest-aware tests:"
echo "    packages/scraper-framework/tests/courts/test_courtlistener.py"
echo "    packages/scraper-framework/tests/courts/ca/test_cc_tentatives_portal.py"
echo "    packages/scraper-framework/tests/courts/test_sf_civil_tentatives.py"
echo ""
echo "  Violating call sites:"
echo ""
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    echo "    $entry"
    violations=$((violations + 1))
done <<< "$python_output"

if (( violations > 0 )); then
    echo ""
    echo "  Found $violations inline reingest-shape CapturedDocument(...) construction(s)."
    exit 1
fi

exit 0
