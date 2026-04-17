#!/usr/bin/env bash
# test_check_no_ecs_wait_services_stable.sh — Tests for check-no-ecs-wait-services-stable.sh
#
# Creates temporary files to verify that the checker correctly detects
# forbidden `aws ecs wait services-stable` usage while allowing comments,
# docs/investigations/ post-mortems, and the check script itself to
# reference the pattern as context.
#
# Usage:
#   scripts/tests/test_check_no_ecs_wait_services_stable.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-ecs-wait-services-stable.sh"
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

# ─── Test 1: Bare shell call should fail ─────────────────────────────────
create_test_file "bad1.sh" 'aws ecs wait services-stable --cluster foo --services bar'
assert_fails "Bare shell call to aws ecs wait services-stable is detected"
reset_tmpdir

# ─── Test 2: YAML usage (composite action) should fail ───────────────────
create_test_file "action.yml" 'runs:
  using: composite
  steps:
    - shell: bash
      run: |
        aws ecs wait services-stable --cluster "$CLUSTER" --services "$SVC"'
assert_fails "YAML composite action call is detected"
reset_tmpdir

# ─── Test 3: Extra whitespace between words should still match ───────────
create_test_file "bad_spaces.sh" 'aws    ecs   wait    services-stable --cluster foo'
assert_fails "Extra whitespace between words is detected"
reset_tmpdir

# ─── Test 4: Tab whitespace between words should still match ─────────────
printf 'aws\tecs\twait\tservices-stable --cluster foo\n' > "$TMPDIR_TEST/bad_tabs.sh"
assert_fails "Tab-separated words are detected"
reset_tmpdir

# ─── Test 5: Shell comment lines should pass ─────────────────────────────
create_test_file "redeploy.sh" '#!/usr/bin/env bash
# Replaces `aws ecs wait services-stable` with a deployment-scoped waiter.
# See issue #2523 for the full motivation.
scripts/wait-for-rollout.sh --cluster foo --service bar'
assert_passes "Shell comments referencing the pattern as context are allowed"
reset_tmpdir

# ─── Test 6: YAML comment lines should pass ──────────────────────────────
create_test_file "action.yml" 'runs:
  using: composite
  steps:
    - shell: bash
      run: |
        # This replaces `aws ecs wait services-stable` with wait-for-rollout.
        scripts/wait-for-rollout.sh --cluster foo'
assert_passes "YAML comments referencing the pattern as context are allowed"
reset_tmpdir

# ─── Test 7: docs/investigations/*.md should pass ───────────────────────
create_test_file "docs/investigations/ecs-waiter-retirement-2025-11.md" '# ECS waiter retirement

Previously the composite action called `aws ecs wait services-stable` which
had a hard-coded 10-minute timeout.  This doc records the migration to
`scripts/wait-for-rollout.sh`.
'
assert_passes "docs/investigations/*.md post-mortems are allowed to reference the pattern"
reset_tmpdir

# ─── Test 8: Non-comment Python call (subprocess) should fail ────────────
create_test_file "bad_py.py" 'import subprocess
subprocess.run(["aws", "ecs", "wait", "services-stable", "--cluster", "foo"])'
assert_passes "Python subprocess list form (no literal whitespace between words) does not match"
reset_tmpdir
# Note: the regex intentionally only matches the shell-form
# `aws ecs wait services-stable` (whitespace-separated tokens in a single
# string).  Catching argv-list forms would require parsing every language,
# which is out of scope — the goal is to block the shell patterns callers
# actually used.

# ─── Test 9: Empty directory should pass ─────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 10: Non-matching shell content should pass ────────────────────
create_test_file "good.sh" 'scripts/wait-for-rollout.sh --cluster foo --service bar'
assert_passes "Shell script that uses the replacement passes"
reset_tmpdir

# ─── Test 11: Pattern inside a different AWS command should not match ───
create_test_file "other.sh" 'aws ecs list-services --cluster foo
aws ecs wait tasks-stopped --cluster foo --tasks t1'
assert_passes "Other 'aws ecs wait' waiters (e.g. tasks-stopped) do not match"
reset_tmpdir

# ─── Test 12: .git directory should be excluded ──────────────────────────
mkdir -p "$TMPDIR_TEST/.git/objects"
printf 'aws ecs wait services-stable\n' > "$TMPDIR_TEST/.git/objects/badfile"
assert_passes ".git directory contents are excluded"
reset_tmpdir

# ─── Test 13: node_modules should be excluded ────────────────────────────
mkdir -p "$TMPDIR_TEST/node_modules/some-pkg"
printf 'aws ecs wait services-stable\n' > "$TMPDIR_TEST/node_modules/some-pkg/index.js"
assert_passes "node_modules directory is excluded"
reset_tmpdir

# ─── Test 14: Indented comment with the pattern should pass ──────────────
create_test_file "indented.sh" '    # Legacy: aws ecs wait services-stable was used here.
    scripts/wait-for-rollout.sh --cluster foo'
assert_passes "Indented comment lines are allowed"
reset_tmpdir

# ─── Test 15: Mixed file — comment OK, code line fails ───────────────────
create_test_file "mixed.sh" '#!/usr/bin/env bash
# Previously: aws ecs wait services-stable --cluster foo
aws ecs wait services-stable --cluster foo --services bar'
assert_fails "Mixed file: code line is caught even when a comment mentions it"
reset_tmpdir

# ─── Test 16: Multiple violations in one file are detected ──────────────
create_test_file "multi.sh" 'aws ecs wait services-stable --cluster a --services b
aws ecs wait services-stable --cluster c --services d'
assert_fails "Multiple code-level violations are detected"
reset_tmpdir

# ─── Test 17: Similar-but-not-matching identifier should pass ───────────
# `aws-ecs-wait-services-stable` as a filename or variable is not the AWS
# CLI invocation and should not trigger the check.
create_test_file "lookalike.sh" 'SCRIPT=aws-ecs-wait-services-stable.sh
echo "$SCRIPT"'
assert_passes "Hyphenated lookalike identifier does not trigger the check"
reset_tmpdir

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
