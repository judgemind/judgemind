#!/usr/bin/env bash
# test_check_aws_flag_portability.sh — Tests for check-aws-flag-portability.sh
#
# Creates temporary shell scripts to verify that the checker correctly
# detects AWS CLI v2-only flags (e.g. --no-cli-pager) in shell scripts.
#
# Usage:
#   scripts/tests/test_check_aws_flag_portability.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-aws-flag-portability.sh"
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

# ─── Test 1: --no-cli-pager in aws call should fail ──────────────────────
create_test_script "bad1.sh" 'aws ecs describe-tasks --cluster foo --no-cli-pager'
assert_fails "--no-cli-pager in aws call is detected"
reset_tmpdir

# ─── Test 2: --cli-pager in aws call should fail ─────────────────────────
create_test_script "bad2.sh" 'aws ecs describe-tasks --cluster foo --cli-pager'
assert_fails "--cli-pager in aws call is detected"
reset_tmpdir

# ─── Test 3: --no-cli-pager at end of line should fail ───────────────────
create_test_script "bad3.sh" 'aws s3 ls --no-cli-pager'
assert_fails "--no-cli-pager at end of line is detected"
reset_tmpdir

# ─── Test 4: --no-cli-pager in array should fail ─────────────────────────
create_test_script "bad4.sh" '#!/bin/bash
args=(--output json --no-cli-pager)
aws ecs describe-tasks "${args[@]}"'
assert_fails "--no-cli-pager in array literal is detected"
reset_tmpdir

# ─── Test 5: --no-cli-pager with output flag after should fail ───────────
create_test_script "bad5.sh" 'aws logs filter-log-events --no-cli-pager --output json'
assert_fails "--no-cli-pager followed by another flag is detected"
reset_tmpdir

# ─── Test 6: Clean script without v2-only flags should pass ──────────────
create_test_script "good1.sh" '#!/bin/bash
export AWS_PAGER=""
aws ecs describe-tasks --cluster foo --output json'
assert_passes "Clean script with AWS_PAGER= passes"
reset_tmpdir

# ─── Test 7: --no-cli-pager in a comment should pass ─────────────────────
create_test_script "comment1.sh" '# aws ecs describe-tasks --no-cli-pager'
assert_passes "--no-cli-pager in comment is ignored"
reset_tmpdir

# ─── Test 8: --no-cli-pager in echo output should pass ───────────────────
create_test_script "echo1.sh" 'echo "remove --no-cli-pager and use AWS_PAGER= instead"'
assert_passes "--no-cli-pager in echo output is ignored"
reset_tmpdir

# ─── Test 9: --no-cli-pager in printf output should pass ─────────────────
create_test_script "printf1.sh" 'printf "Fix: remove --no-cli-pager\n"'
assert_passes "--no-cli-pager in printf output is ignored"
reset_tmpdir

# ─── Test 10: Non-.sh files are ignored ──────────────────────────────────
printf '%s\n' 'aws ecs describe-tasks --no-cli-pager' > "$TMPDIR_TEST/bad.py"
assert_passes "Non-.sh files are ignored"
rm -f "$TMPDIR_TEST/bad.py"

# ─── Test 11: Empty directory should pass ────────────────────────────────
assert_passes "Empty directory with no scripts passes"

# ─── Test 12: Flag in code with preceding comment should still fail ───────
create_test_script "mixed1.sh" '#!/bin/bash
# This is a comment about --no-cli-pager
aws ecs describe-tasks --no-cli-pager --output json'
assert_fails "Code line with v2-only flag is caught even when file has comments"
reset_tmpdir

# ─── Test 13: No self-match on ci.yml step name ──────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden pattern.  See issue #2542 for the
# class of failure (originally observed on PR #2541 with a different
# guard).  A tempting misname for this guard would be e.g.:
#
#   - name: Check AWS CLI --no-cli-pager usage
#
# which would trip the guard on every CI run.  This guard only scans
# *.sh files, so we write the extracted step names into a .sh tmpfile
# to give the guard something it will actually look at.  See
# scripts/tests/_guard_self_match_helpers.sh for the shared helper.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-aws-flag-portability.sh" "sh"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
