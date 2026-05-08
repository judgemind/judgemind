#!/usr/bin/env bash
# test_check_oneshot_imports.sh — Regression tests for check-oneshot-imports.sh
#
# Creates temporary Python files in scripts/ to verify that the checker
# correctly detects top-level sibling imports and allows indented ones.
#
# Usage:
#   scripts/tests/test_check_oneshot_imports.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-oneshot-imports.sh"
FAILURES=0
TESTS=0

# Cleanup any temp files on exit
TEMP_FILES=()
cleanup() {
    # ``${TEMP_FILES[@]+...}`` guards empty-array iteration under
    # bash 3.2 + set -u (see #4336) — without it, an early test
    # failure that fires the EXIT trap before any
    # create_temp_script call would crash the trap itself.
    for f in ${TEMP_FILES[@]+"${TEMP_FILES[@]}"}; do
        rm -f "$f"
    done
}
trap cleanup EXIT

create_temp_script() {
    local name="$1"
    local content="$2"
    local path="$SCRIPT_DIR/$name"
    printf '%s\n' "$content" > "$path"
    TEMP_FILES+=("$path")
    echo "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Test 1: Top-level 'from sibling import ...' should fail ──────────────
create_temp_script "_test_oneshot_bad1.py" "from dq_trend_storage import store_results"
assert_fails "Top-level 'from sibling import' is detected"
rm -f "$SCRIPT_DIR/_test_oneshot_bad1.py"

# ─── Test 2: Top-level 'import sibling' should fail ──────────────────────
create_temp_script "_test_oneshot_bad2.py" "import dq_trend_storage"
assert_fails "Top-level 'import sibling' is detected"
rm -f "$SCRIPT_DIR/_test_oneshot_bad2.py"

# ─── Test 3: Top-level 'import sibling as ...' should fail ───────────────
create_temp_script "_test_oneshot_bad3.py" "import ralph_review_log as rrl"
assert_fails "Top-level 'import sibling as' is detected"
rm -f "$SCRIPT_DIR/_test_oneshot_bad3.py"

# ─── Test 4: Indented import (lazy) should pass ──────────────────────────
create_temp_script "_test_oneshot_ok1.py" "def load():
    import dq_trend_storage
    return dq_trend_storage"
assert_passes "Indented import inside function is allowed"
rm -f "$SCRIPT_DIR/_test_oneshot_ok1.py"

# ─── Test 5: Indented 'from ... import' (lazy) should pass ───────────────
create_temp_script "_test_oneshot_ok2.py" "def load():
    from dq_trend_storage import store_results
    return store_results"
assert_passes "Indented 'from ... import' inside function is allowed"
rm -f "$SCRIPT_DIR/_test_oneshot_ok2.py"

# ─── Test 6: stdlib import should pass ────────────────────────────────────
create_temp_script "_test_oneshot_ok4.py" "import json
import os
from pathlib import Path"
assert_passes "stdlib imports are allowed"
rm -f "$SCRIPT_DIR/_test_oneshot_ok4.py"

# ─── Test 7: Clean state (no temp files) should pass ─────────────────────
assert_passes "Clean state with no violations passes"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
