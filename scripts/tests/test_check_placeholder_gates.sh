#!/usr/bin/env bash
# test_check_placeholder_gates.sh — Tests for check-placeholder-gates.sh
#
# Creates temporary files to verify that the checker correctly detects
# placeholder security gate patterns while allowing legitimate uses
# (#2884 markers, test paths, innocuous placeholder text).
#
# Usage:
#   scripts/tests/test_check_placeholder_gates.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-placeholder-gates.sh"
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
    printf '%s\n' "$content" > "$TMPDIR_TEST/$name"
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
}

# ─── Test 1: Bare "accept any non-empty MFA header" in TS source fails ─
create_test_file "middleware.ts" '// accept any non-empty MFA header'
assert_fails "accept any non-empty near MFA keyword is detected"
reset_tmpdir

# ─── Test 2: Same line with #2884 reference is exempted ───────────────
create_test_file "middleware.ts" '// accept any non-empty MFA header // see #2884'
assert_passes "accept any non-empty MFA line with #2884 ref is allowed"
reset_tmpdir

# ─── Test 3: Matching line under tests/ directory is exempted ──────────
mkdir -p "$TMPDIR_TEST/tests"
printf '%s\n' '// accept any non-empty MFA header' > "$TMPDIR_TEST/tests/middleware.test.ts"
assert_passes "Matching line under tests/ directory is allowed"
reset_tmpdir

# ─── Test 4: TODO Phase N placeholder fails ───────────────────────────
create_test_file "auth.ts" '// TODO: Phase 2 placeholder -- real auth gate lands in sub-task E'
assert_fails "TODO Phase N placeholder is detected"
reset_tmpdir

# ─── Test 5: Innocuous UI placeholder attribute passes ────────────────
create_test_file "form.tsx" '<input placeholder="Enter your name" />'
assert_passes "Innocuous UI placeholder attribute is allowed"
reset_tmpdir

# ─── Test 6: Innocuous em-dash placeholder for null display passes ─────
create_test_file "display.ts" '// em-dash placeholder for null display'
assert_passes "Innocuous em-dash placeholder for null display is allowed"
reset_tmpdir

# ─── Summary ───────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
