#!/usr/bin/env bash
# test_check_duplicate_functions.sh — Tests for check-duplicate-functions.py
#
# Creates temporary Python files to verify that the checker correctly detects
# duplicate top-level function and class definitions.
#
# Usage:
#   scripts/tests/test_check_duplicate_functions.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-duplicate-functions.py"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local path="$TMPDIR_TEST/$name"
    printf '%s\n' "$content" > "$path"
    echo "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if python3 "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if python3 "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
}

# ─── Test 1: Duplicate function at module scope should fail ──────────────
create_test_file "dup_func.py" 'def foo():
    pass

def bar():
    pass

def foo():
    return 1'
assert_fails "Duplicate top-level function detected"
reset_tmpdir

# ─── Test 2: No duplicates should pass ──────────────────────────────────
create_test_file "clean.py" 'def foo():
    pass

def bar():
    pass

def baz():
    return 1'
assert_passes "No duplicates passes"
reset_tmpdir

# ─── Test 3: Duplicate class at module scope should fail ────────────────
create_test_file "dup_class.py" 'class MyClass:
    pass

class MyClass:
    x = 1'
assert_fails "Duplicate top-level class detected"
reset_tmpdir

# ─── Test 4: Same-name methods in different classes should pass ─────────
create_test_file "methods.py" 'class A:
    def process(self):
        pass

class B:
    def process(self):
        pass'
assert_passes "Same-name methods in different classes pass"
reset_tmpdir

# ─── Test 5: Nested function with same name as outer should pass ───────
create_test_file "nested.py" 'def outer():
    def inner():
        pass
    return inner

def inner():
    pass'
assert_passes "Nested function with same name as module-level passes"
reset_tmpdir

# ─── Test 6: Async function duplicate should fail ──────────────────────
create_test_file "async_dup.py" 'async def fetch():
    pass

async def fetch():
    return None'
assert_fails "Duplicate async function detected"
reset_tmpdir

# ─── Test 7: Mixed sync/async duplicate should fail ────────────────────
create_test_file "mixed_dup.py" 'def handler():
    pass

async def handler():
    return None'
assert_fails "Mixed sync/async duplicate detected"
reset_tmpdir

# ─── Test 8: Empty directory should pass ────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 9: Syntax error file should be skipped (pass) ────────────────
create_test_file "broken.py" 'def foo(
    this is not valid python'
assert_passes "Syntax error file is skipped"
reset_tmpdir

# ─── Test 10: Triple duplicate should fail ──────────────────────────────
create_test_file "triple.py" 'def setup():
    pass

def cleanup():
    pass

def setup():
    return 1

def setup():
    return 2'
assert_fails "Triple definition detected"
reset_tmpdir

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
