#!/usr/bin/env bash
# test_check_llm_paths_symmetry.sh — Tests for check-llm-paths-symmetry.sh
#
# Exercises the dual-LLM-path symmetry guard against synthetic source trees
# under a temp directory.  The check script honors LLM_PATHS_SYMMETRY_ROOT to
# enable this without mutating the real repo.
#
# Usage:
#   scripts/tests/test_check_llm_paths_symmetry.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-llm-paths-symmetry.sh"
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

PATH_A_REL="packages/scraper-framework/src/framework/llm_extractor.py"
PATH_B_REL="packages/scraper-framework/src/ingestion/llm_extract.py"

# Canonical "both paths good" content — both events present, both carry
# document_id=.  Each test case overlays one or both files with a variant.
GOOD_A='import logging
logger = logging.getLogger(__name__)

def extract_chunk():
    logger.warning(
        "llm_extractor.google_api_failure",
        chunk_index=0,
        document_id="abc123",
        provider="google",
    )
'

GOOD_B='import logging
logger = logging.getLogger(__name__)

def extract_chunk():
    logger.warning(
        "llm_extract.chunk_api_failure",
        chunk_index=0,
        document_id="abc123",
        provider="anthropic",
    )
'

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if LLM_PATHS_SYMMETRY_ROOT="$TMPDIR_TEST" "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if LLM_PATHS_SYMMETRY_ROOT="$TMPDIR_TEST" "$CHECK_SCRIPT" > /dev/null 2>&1; then
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

# ─── Test 1: both paths good — script passes ──────────────────────────
write_module "$PATH_A_REL" "$GOOD_A"
write_module "$PATH_B_REL" "$GOOD_B"
assert_passes "both paths emit canonical event with document_id="
reset_tmpdir

# ─── Test 2: path A missing the event entirely — script fails ─────────
write_module "$PATH_A_REL" 'import logging
logger = logging.getLogger(__name__)

def extract_chunk():
    logger.warning("some_other_event", document_id="abc")
'
write_module "$PATH_B_REL" "$GOOD_B"
assert_fails "missing google_api_failure in framework/llm_extractor.py is detected"
reset_tmpdir

# ─── Test 3: path B missing the event entirely — script fails ─────────
write_module "$PATH_A_REL" "$GOOD_A"
write_module "$PATH_B_REL" 'import logging
logger = logging.getLogger(__name__)

def extract_chunk():
    logger.warning("some_other_event", document_id="abc")
'
assert_fails "missing chunk_api_failure in ingestion/llm_extract.py is detected"
reset_tmpdir

# ─── Test 4: path A emits event but missing document_id — script fails ─
write_module "$PATH_A_REL" 'import logging
logger = logging.getLogger(__name__)

def extract_chunk():
    logger.warning(
        "llm_extractor.google_api_failure",
        chunk_index=0,
        provider="google",
    )
'
write_module "$PATH_B_REL" "$GOOD_B"
assert_fails "google_api_failure without document_id= is detected"
reset_tmpdir

# ─── Test 5: path B emits event but missing document_id — script fails ─
write_module "$PATH_A_REL" "$GOOD_A"
write_module "$PATH_B_REL" 'import logging
logger = logging.getLogger(__name__)

def extract_chunk():
    logger.warning(
        "llm_extract.chunk_api_failure",
        chunk_index=0,
        provider="anthropic",
    )
'
assert_fails "chunk_api_failure without document_id= is detected"
reset_tmpdir

# ─── Test 6: both files missing — script fails ────────────────────────
mkdir -p "$TMPDIR_TEST/packages/scraper-framework/src/framework"
mkdir -p "$TMPDIR_TEST/packages/scraper-framework/src/ingestion"
assert_fails "missing module files are detected"
reset_tmpdir

# ─── Test 7: docstring/comment mention of the event name does NOT  ────
# count as an emission — the check requires the trailing comma form
# that occurs only inside the logger.warning() call.
write_module "$PATH_A_REL" 'import logging
logger = logging.getLogger(__name__)

# This file used to emit llm_extractor.google_api_failure but no longer does.
def extract_chunk():
    pass
'
write_module "$PATH_B_REL" "$GOOD_B"
assert_fails "comment mention of event name is not counted as an emission"
reset_tmpdir

# ─── Test 8: multiple emissions, one missing document_id — fails ──────
write_module "$PATH_A_REL" 'import logging
logger = logging.getLogger(__name__)

def extract_chunk_a():
    logger.warning(
        "llm_extractor.google_api_failure",
        chunk_index=0,
        document_id="abc123",
    )

def extract_chunk_b():
    logger.warning(
        "llm_extractor.google_api_failure",
        chunk_index=1,
        provider="google",
    )
'
write_module "$PATH_B_REL" "$GOOD_B"
assert_fails "second emission missing document_id= is detected"
reset_tmpdir

# ─── Test 9: multiple emissions, all carrying document_id — passes ────
write_module "$PATH_A_REL" 'import logging
logger = logging.getLogger(__name__)

def extract_chunk_a():
    logger.warning(
        "llm_extractor.google_api_failure",
        chunk_index=0,
        document_id="abc123",
    )

def extract_chunk_b():
    logger.warning(
        "llm_extractor.google_api_failure",
        chunk_index=1,
        document_id="def456",
    )
'
write_module "$PATH_B_REL" "$GOOD_B"
assert_passes "multiple emissions all carrying document_id= is allowed"
reset_tmpdir

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
