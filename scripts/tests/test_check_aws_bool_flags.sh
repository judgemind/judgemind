#!/usr/bin/env bash
# test_check_aws_bool_flags.sh — Tests for check-aws-bool-flags.sh
#
# Creates temporary shell scripts to verify that the checker correctly
# detects AWS CLI boolean flags followed by true/false value arguments.
#
# Usage:
#   scripts/tests/test_check_aws_bool_flags.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-aws-bool-flags.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute scripts/
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_script() {
    local name="$1"
    local content="$2"
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
    rm -f "$TMPDIR_TEST"/*.sh
}

# ─── Test 1: --start-from-head false should fail ─────────────────────────
create_test_script "bad1.sh" 'aws logs get-log-events --start-from-head false'
assert_fails "--start-from-head false is detected"
reset_tmpdir

# ─── Test 2: --start-from-head true should fail ──────────────────────────
create_test_script "bad2.sh" 'aws logs get-log-events --start-from-head true'
assert_fails "--start-from-head true is detected"
reset_tmpdir

# ─── Test 3: --no-paginate true should fail ──────────────────────────────
create_test_script "bad3.sh" 'aws s3 ls --no-paginate true'
assert_fails "--no-paginate true is detected"
reset_tmpdir

# ─── Test 4: --no-cli-pager false should fail ────────────────────────────
create_test_script "bad4.sh" 'aws ecs describe-tasks --no-cli-pager false'
assert_fails "--no-cli-pager false is detected"
reset_tmpdir

# ─── Test 5: --descending false should fail ──────────────────────────────
create_test_script "bad5.sh" 'aws logs describe-log-streams --descending false'
assert_fails "--descending false is detected"
reset_tmpdir

# ─── Test 6: --cli-pager false should fail ───────────────────────────────
create_test_script "bad6.sh" 'aws ecs list-tasks --cli-pager false'
assert_fails "--cli-pager false is detected"
reset_tmpdir

# ─── Test 7: Correct usage --no-start-from-head should pass ─────────────
create_test_script "good1.sh" 'aws logs get-log-events --no-start-from-head --no-cli-pager'
assert_passes "--no-start-from-head (without value) passes"
reset_tmpdir

# ─── Test 8: Correct usage --start-from-head (bare flag) should pass ─────
create_test_script "good2.sh" 'aws logs get-log-events --start-from-head --no-cli-pager'
assert_passes "--start-from-head (without value) passes"
reset_tmpdir

# ─── Test 9: Correct usage --descending should pass ──────────────────────
create_test_script "good3.sh" 'aws logs describe-log-streams --descending --no-cli-pager'
assert_passes "--descending (without value) passes"
reset_tmpdir

# ─── Test 10: Correct usage --no-paginate should pass ────────────────────
create_test_script "good4.sh" 'aws s3 ls --no-paginate --output json'
assert_passes "--no-paginate (without value) passes"
reset_tmpdir

# ─── Test 11: Empty directory should pass ─────────────────────────────────
assert_passes "Empty directory with no scripts passes"

# ─── Test 12: Flag in a comment should pass (comments are not code) ───────
create_test_script "comment1.sh" '# aws logs get-log-events --start-from-head false'
assert_passes "Flag in comment is ignored"
reset_tmpdir

# ─── Test 16: Flag in echo/printf should pass (help text, not code) ──────
create_test_script "echo1.sh" 'echo "    BAD:  --start-from-head false"'
assert_passes "Flag in echo output is ignored"
reset_tmpdir

# ─── Test 17: Flag in code with indented comment should fail ─────────────
create_test_script "mixed1.sh" '#!/bin/bash
# This is a comment about --start-from-head
aws logs get-log-events --start-from-head false'
assert_fails "Code line with bad flag is caught even when file has comments"
reset_tmpdir

# ─── Test 13: Multiple flags in one file ──────────────────────────────────
create_test_script "multi.sh" 'aws logs get-log-events --start-from-head false --no-cli-pager true'
assert_fails "Multiple bad flags in one file are detected"
reset_tmpdir

# ─── Test 14: --dry-run false should fail ─────────────────────────────────
create_test_script "bad_dryrun.sh" 'aws ec2 run-instances --dry-run false'
assert_fails "--dry-run false is detected"
reset_tmpdir

# ─── Test 15: Non-.sh files are ignored ───────────────────────────────────
printf '%s\n' 'aws logs get-log-events --start-from-head false' > "$TMPDIR_TEST/bad.py"
assert_passes "Non-.sh files are ignored"
rm -f "$TMPDIR_TEST/bad.py"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
