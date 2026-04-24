#!/usr/bin/env bash
# test_install_dispatcher_venv.sh — smoke tests for install-dispatcher-venv.sh
#
# These are structural tests that don't run `pip install` against the
# network. We verify:
#   1. Script exists and is executable
#   2. Usage error on any argument (takes none — exit 1 with usage message)
#   3. --help / -h flags exit non-zero with a usage message (unknown args rejected)
#
# Usage: scripts/tests/test_install_dispatcher_venv.sh
#
# Exits 0 if all tests pass, 1 if any test fails.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$SCRIPT_DIR/install-dispatcher-venv.sh"

if [[ ! -x "$SCRIPT" ]]; then
    echo "ERROR: script not executable: $SCRIPT" >&2
    exit 1
fi

PASS=0
FAIL=0

assert_exit_code() {
    local expected="$1"
    local actual="$2"
    local msg="$3"
    if [[ "$actual" == "$expected" ]]; then
        echo "PASS: $msg (exit $actual)"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $msg (expected exit $expected, got $actual)"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local haystack="$1"
    local needle="$2"
    local msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        echo "PASS: $msg"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $msg (expected to contain '$needle', got: $haystack)"
        FAIL=$((FAIL + 1))
    fi
}

# --- Test 1: script exists and is executable ---
echo "--- Test 1: script exists and is executable ---"
if [[ -x "$SCRIPT" ]]; then
    echo "PASS: script exists and is executable"
    PASS=$((PASS + 1))
else
    echo "FAIL: script missing or not executable: $SCRIPT"
    FAIL=$((FAIL + 1))
fi

# --- Test 2: any argument → usage error (exit 1) ---
echo "--- Test 2: any argument → usage error ---"
OUTPUT=$("$SCRIPT" some-arg 2>&1 || true)
"$SCRIPT" some-arg >/dev/null 2>&1 && RC=0 || RC=$?
assert_exit_code 1 "$RC" "argument rejected with exit 1"
assert_contains "$OUTPUT" "Usage:" "usage message printed on unexpected arg"

# --- Test 3: --help flag → exits non-zero with usage message ---
echo "--- Test 3: --help → usage message ---"
OUTPUT=$("$SCRIPT" --help 2>&1 || true)
"$SCRIPT" --help >/dev/null 2>&1 && RC=0 || RC=$?
assert_exit_code 1 "$RC" "--help exits non-zero (unknown args rejected)"
assert_contains "$OUTPUT" "Usage:" "usage message printed for --help"

# --- Summary ---
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
