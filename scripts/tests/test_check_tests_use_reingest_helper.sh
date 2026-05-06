#!/usr/bin/env bash
# test_check_tests_use_reingest_helper.sh — Tests for
# check-tests-use-reingest-helper.sh (issue #4190).
#
# Mirrors the structure of test_check_parse_document_reingest_safety.sh
# (#4141): synthesizes Python files exercising the scanner's
# pass-and-fail cases — reingest-shape inline construction (fails),
# fully-populated cap_doc (passes), helper-routed call (passes), etc.
#
# Usage:
#   scripts/tests/test_check_tests_use_reingest_helper.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-tests-use-reingest-helper.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local dir
    dir="$(dirname "$TMPDIR_TEST/$name")"
    mkdir -p "$dir"
    local path="$TMPDIR_TEST/$name"
    printf '%s\n' "$content" > "$path"
    echo "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test 1: Reingest-shape inline call FAILS ────────────────────────────
create_test_file "reingest_shape.py" 'from framework import CapturedDocument, ContentFormat
from datetime import datetime, UTC

def make():
    return CapturedDocument(
        document_id="d1",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Test Superior Court",
        source_url="https://example.com/x",
        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"hello",
        content_hash="deadbeef" * 8,
    )
' > /dev/null
assert_fails "Reingest-shape inline CapturedDocument fails"
reset_tmpdir

# ─── Test 2: Fully-populated cap_doc PASSES ──────────────────────────────
create_test_file "fully_populated.py" 'from framework import CapturedDocument, ContentFormat
from datetime import datetime, UTC

def make():
    return CapturedDocument(
        document_id="d1",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Test Superior Court",
        source_url="https://example.com/x",
        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"hello",
        content_hash="deadbeef" * 8,
        case_number="ABC-123",
        judge_name="Hon. Test Judge",
        hearing_date=datetime(2026, 2, 1, tzinfo=UTC),
    )
' > /dev/null
assert_passes "Fully-populated CapturedDocument (case_number, judge_name) passes"
reset_tmpdir

# ─── Test 3: Helper-routed call PASSES ───────────────────────────────────
create_test_file "helper_call.py" 'from helpers.reingest import make_reingest_cap_doc

def make():
    return make_reingest_cap_doc(
        raw_content=b"hello",
        scraper_id="ca-test",
    )
' > /dev/null
assert_passes "make_reingest_cap_doc(...) passes (not a CapturedDocument call)"
reset_tmpdir

# ─── Test 4: Call passing extra (parsed-set) field FAILS as PASSES ───────
# A call passing only ``extra={...}`` plus identifiers is NOT reingest
# shape — ``extra`` is in the parsed-fields set, so its presence
# excludes the call from the violation set.
create_test_file "with_extra.py" 'from framework import CapturedDocument, ContentFormat
from datetime import datetime, UTC

def make():
    return CapturedDocument(
        document_id="d1",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Test Superior Court",
        source_url="https://example.com/x",
        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"hello",
        content_hash="deadbeef" * 8,
        extra={"flag": True},
    )
' > /dev/null
assert_passes "CapturedDocument(..., extra={...}) is not reingest-shape"
reset_tmpdir

# ─── Test 5: Partial identifier set PASSES ───────────────────────────────
# Missing one of the identifier fields means the call is not a complete
# reingest-shape — it's a custom partial construction.
create_test_file "partial.py" 'from framework import CapturedDocument, ContentFormat
from datetime import datetime, UTC

def make():
    return CapturedDocument(
        document_id="d1",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Test Superior Court",
        source_url="https://example.com/x",
        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"hello",
    )
' > /dev/null
assert_passes "Partial identifier set (missing content_hash) passes"
reset_tmpdir

# ─── Test 6: ``**kwargs`` skipped silently ───────────────────────────────
create_test_file "starred.py" 'from framework import CapturedDocument

def make(**kwargs):
    return CapturedDocument(**kwargs)
' > /dev/null
assert_passes "**kwargs CapturedDocument call skipped"
reset_tmpdir

# ─── Test 7: Multiple violations in one file FAIL ────────────────────────
create_test_file "multi.py" 'from framework import CapturedDocument, ContentFormat
from datetime import datetime, UTC

def make_a():
    return CapturedDocument(
        document_id="a",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Test Superior Court",
        source_url="https://example.com/a",
        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"a",
        content_hash="aa",
    )

def make_b():
    return CapturedDocument(
        document_id="b",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Test Superior Court",
        source_url="https://example.com/b",
        capture_timestamp=datetime(2026, 1, 2, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"b",
        content_hash="bb",
    )
' > /dev/null
assert_fails "Multiple inline reingest-shape calls in one file fail"
reset_tmpdir

# ─── Test 8: Mixed pass+fail tree fails ──────────────────────────────────
create_test_file "good_and_bad/good.py" 'from helpers.reingest import make_reingest_cap_doc

def make():
    return make_reingest_cap_doc(raw_content=b"x", scraper_id="ca-test")
' > /dev/null
create_test_file "good_and_bad/bad.py" 'from framework import CapturedDocument, ContentFormat
from datetime import datetime, UTC

def make():
    return CapturedDocument(
        document_id="d1",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Test Superior Court",
        source_url="https://example.com/x",
        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"hello",
        content_hash="deadbeef" * 8,
    )
' > /dev/null
assert_fails "Mixed pass+fail tree fails because of the bad file"
reset_tmpdir

# ─── Test 9: Empty file passes ───────────────────────────────────────────
create_test_file "empty.py" '"""Empty module."""
' > /dev/null
assert_passes "Empty module passes"
reset_tmpdir

# ─── Test 10: Syntactically broken Python is skipped silently ────────────
create_test_file "broken.py" 'class S(:  # syntax error
    pass
' > /dev/null
assert_passes "Syntactically broken Python is skipped silently"
reset_tmpdir

# ─── Test 11: AST scan ignores .venv/ subdir ─────────────────────────────
mkdir -p "$TMPDIR_TEST/.venv/lib/python3.12/site-packages"
printf '%s\n' 'from framework import CapturedDocument, ContentFormat
from datetime import datetime, UTC

def make():
    return CapturedDocument(
        document_id="d1",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Test Superior Court",
        source_url="https://example.com/x",
        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"hello",
        content_hash="deadbeef" * 8,
    )
' > "$TMPDIR_TEST/.venv/lib/python3.12/site-packages/vendored.py"
assert_passes ".venv/ subdir is excluded from the scan"
reset_tmpdir

# ─── Test 12: framework.CapturedDocument(...) attribute call FAILS ───────
create_test_file "attr.py" 'import framework
from framework import ContentFormat
from datetime import datetime, UTC

def make():
    return framework.CapturedDocument(
        document_id="d1",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Test Superior Court",
        source_url="https://example.com/x",
        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"hello",
        content_hash="deadbeef" * 8,
    )
' > /dev/null
assert_fails "Attribute call framework.CapturedDocument(...) is detected"
reset_tmpdir

# ─── Test 13: Direct production-tree run reports zero violations ─────────
# Defense-in-depth — the production tree (the default scan root) is
# clean today (after #4153, #4133, #4165 migrated the three known
# consumers to the helper).
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: Production tests/courts/ tree passes the check"
else
    echo "FAIL: Production tests/courts/ tree fails the check (expected pass)"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 14: No self-match on ci.yml step name ──────────────────────────
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-tests-use-reingest-helper.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
