#!/usr/bin/env bash
# test_check_case_type_fallback_parity.sh — Tests for
# check-case-type-fallback-parity.sh.
#
# Exercises the case_type fallback parity guard against synthetic source
# trees under a temp directory.  The check script honors
# ``CASE_TYPE_PARITY_ROOT`` to enable this without mutating the real repo.
#
# Usage:
#   scripts/tests/test_check_case_type_fallback_parity.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-case-type-fallback-parity.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

write_module() {
    local rel="$1"
    local content="$2"
    local path="$TMPDIR_TEST/$rel"
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" > "$path"
}

WORKER_REL="packages/scraper-framework/src/ingestion/worker.py"
REINGEST_REL="scripts/reingest_from_s3.py"

# Canonical "both paths in sync" fixture — the four helpers that ship in
# main today (number, scraper_id, motion_type, title).  Each test case
# overlays one or both files with a variant that mutates this fixture.
GOOD_WORKER='from extractors import (
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
)


def enrich(case_number, scraper_id, motion_type, title):
    case_type = None
    if case_number:
        case_type = extract_case_type_from_number(case_number)
    if case_type is None and scraper_id:
        case_type = extract_case_type_from_scraper_id(scraper_id)
    if case_type is None and motion_type:
        case_type = extract_case_type_from_motion_type(motion_type)
    if case_type is None and title:
        case_type = extract_case_type_from_title(title)
    return case_type
'

GOOD_REINGEST='from extractors import (
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
)


def _apply_regex_fallbacks(extracted, text, scraper_id=""):
    if not extracted["case_type"] and extracted["case_number"]:
        val = extract_case_type_from_number(extracted["case_number"])
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"] and scraper_id:
        val = extract_case_type_from_scraper_id(scraper_id)
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"] and extracted["motion_type"]:
        val = extract_case_type_from_motion_type(extracted["motion_type"])
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"] and extracted.get("case_title"):
        val = extract_case_type_from_title(extracted["case_title"])
        if val:
            extracted["case_type"] = val
'

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if CASE_TYPE_PARITY_ROOT="$TMPDIR_TEST" "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if CASE_TYPE_PARITY_ROOT="$TMPDIR_TEST" "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test 1: both paths in sync — script passes ────────────────────────
write_module "$WORKER_REL" "$GOOD_WORKER"
write_module "$REINGEST_REL" "$GOOD_REINGEST"
assert_passes "matched fallback chain in worker.py and _apply_regex_fallbacks"
reset_tmpdir

# ─── Test 2: worker.py has a fallback that reingest is missing ─────────
# This is the #4263 / #2062 shape — extract_case_type_from_title was
# added to worker.py but the corresponding call never landed in
# _apply_regex_fallbacks.  Reingest is missing extract_case_type_from_title.
WORKER_WITH_TITLE='from extractors import (
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
)


def enrich(case_number, scraper_id, motion_type, title):
    case_type = None
    if case_number:
        case_type = extract_case_type_from_number(case_number)
    if case_type is None and scraper_id:
        case_type = extract_case_type_from_scraper_id(scraper_id)
    if case_type is None and motion_type:
        case_type = extract_case_type_from_motion_type(motion_type)
    if case_type is None and title:
        case_type = extract_case_type_from_title(title)
    return case_type
'

REINGEST_WITHOUT_TITLE='from extractors import (
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
)


def _apply_regex_fallbacks(extracted, text, scraper_id=""):
    if not extracted["case_type"] and extracted["case_number"]:
        val = extract_case_type_from_number(extracted["case_number"])
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"] and scraper_id:
        val = extract_case_type_from_scraper_id(scraper_id)
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"] and extracted["motion_type"]:
        val = extract_case_type_from_motion_type(extracted["motion_type"])
        if val:
            extracted["case_type"] = val
'
write_module "$WORKER_REL" "$WORKER_WITH_TITLE"
write_module "$REINGEST_REL" "$REINGEST_WITHOUT_TITLE"
assert_fails "worker has extract_case_type_from_title but reingest does not (#4263 shape)"
reset_tmpdir

# ─── Test 3: reingest has a fallback that worker.py is missing ─────────
# Symmetric case — someone adds a new fallback to reingest but forgets
# to add it to worker.py.
WORKER_WITHOUT_PARTY='from extractors import (
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
)


def enrich(case_number, scraper_id, motion_type, title):
    case_type = None
    if case_number:
        case_type = extract_case_type_from_number(case_number)
    if case_type is None and scraper_id:
        case_type = extract_case_type_from_scraper_id(scraper_id)
    if case_type is None and motion_type:
        case_type = extract_case_type_from_motion_type(motion_type)
    if case_type is None and title:
        case_type = extract_case_type_from_title(title)
    return case_type
'

REINGEST_WITH_PARTY='from extractors import (
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_party,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
)


def _apply_regex_fallbacks(extracted, text, scraper_id=""):
    if not extracted["case_type"] and extracted["case_number"]:
        val = extract_case_type_from_number(extracted["case_number"])
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"] and scraper_id:
        val = extract_case_type_from_scraper_id(scraper_id)
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"] and extracted["motion_type"]:
        val = extract_case_type_from_motion_type(extracted["motion_type"])
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"] and extracted.get("case_title"):
        val = extract_case_type_from_title(extracted["case_title"])
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"]:
        val = extract_case_type_from_party(extracted.get("party_name"))
        if val:
            extracted["case_type"] = val
'
write_module "$WORKER_REL" "$WORKER_WITHOUT_PARTY"
write_module "$REINGEST_REL" "$REINGEST_WITH_PARTY"
assert_fails "reingest has extract_case_type_from_party but worker does not"
reset_tmpdir

# ─── Test 4: both paths add the new fallback symmetrically — passes ────
WORKER_WITH_PARTY='from extractors import (
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_party,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
)


def enrich(case_number, scraper_id, motion_type, title, party):
    case_type = None
    if case_number:
        case_type = extract_case_type_from_number(case_number)
    if case_type is None and scraper_id:
        case_type = extract_case_type_from_scraper_id(scraper_id)
    if case_type is None and motion_type:
        case_type = extract_case_type_from_motion_type(motion_type)
    if case_type is None and title:
        case_type = extract_case_type_from_title(title)
    if case_type is None and party:
        case_type = extract_case_type_from_party(party)
    return case_type
'
write_module "$WORKER_REL" "$WORKER_WITH_PARTY"
write_module "$REINGEST_REL" "$REINGEST_WITH_PARTY"
assert_passes "new fallback added to both paths symmetrically"
reset_tmpdir

# ─── Test 5: import-only reference does NOT count as a usage ───────────
# A helper that is imported but never CALLED is still a divergence — the
# fallback chain isn't actually wiring it.  This test asserts that the
# guard counts USES, not imports.
WORKER_IMPORTS_BUT_DOES_NOT_CALL_TITLE='from extractors import (
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
)


def enrich(case_number, scraper_id, motion_type, title):
    case_type = None
    if case_number:
        case_type = extract_case_type_from_number(case_number)
    if case_type is None and scraper_id:
        case_type = extract_case_type_from_scraper_id(scraper_id)
    if case_type is None and motion_type:
        case_type = extract_case_type_from_motion_type(motion_type)
    # extract_case_type_from_title is imported but never called below.
    return case_type
'
write_module "$WORKER_REL" "$WORKER_IMPORTS_BUT_DOES_NOT_CALL_TITLE"
write_module "$REINGEST_REL" "$GOOD_REINGEST"
assert_fails "worker imports extract_case_type_from_title but never calls it"
reset_tmpdir

# ─── Test 6: missing _apply_regex_fallbacks function — script fails ────
write_module "$WORKER_REL" "$GOOD_WORKER"
write_module "$REINGEST_REL" 'from extractors import extract_case_type_from_number


def some_other_function():
    return extract_case_type_from_number("ABC123")
'
assert_fails "missing _apply_regex_fallbacks function in reingest_from_s3.py"
reset_tmpdir

# ─── Test 7: missing source files — script fails ──────────────────────
mkdir -p "$TMPDIR_TEST/packages/scraper-framework/src/ingestion"
mkdir -p "$TMPDIR_TEST/scripts"
assert_fails "missing source files are detected"
reset_tmpdir

# ─── Test 8: helper called only OUTSIDE _apply_regex_fallbacks fails ───
# The reingest module has many other functions (_reparse_document, etc.)
# that may reference the helpers.  When worker.py uses a helper that is
# referenced ONLY outside _apply_regex_fallbacks (e.g. in a stale doc
# path or unrelated helper), the parity check MUST still fail — the
# fallback chain inside _apply_regex_fallbacks is what makes reingest
# produce the same case_type as worker.py.  This is the inverse of test
# 5 for the reingest side.
write_module "$WORKER_REL" "$GOOD_WORKER"
write_module "$REINGEST_REL" 'from extractors import (
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
)


def _apply_regex_fallbacks(extracted, text, scraper_id=""):
    # NOTE: extract_case_type_from_title intentionally absent here —
    # it is referenced only in some_unrelated_helper below, which is
    # NOT called from the reingest fallback chain.
    if not extracted["case_type"] and extracted["case_number"]:
        val = extract_case_type_from_number(extracted["case_number"])
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"] and scraper_id:
        val = extract_case_type_from_scraper_id(scraper_id)
        if val:
            extracted["case_type"] = val
    if not extracted["case_type"] and extracted["motion_type"]:
        val = extract_case_type_from_motion_type(extracted["motion_type"])
        if val:
            extracted["case_type"] = val


def some_unrelated_helper(case_title):
    # A reference to extract_case_type_from_title here MUST NOT make the
    # parity check pass — only the call set inside
    # _apply_regex_fallbacks counts.
    return extract_case_type_from_title(case_title)
'
assert_fails "helper called only outside _apply_regex_fallbacks does not satisfy parity"
reset_tmpdir

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
