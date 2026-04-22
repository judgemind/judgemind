#!/usr/bin/env bash
# test_check_no_test_database_url_fallback.sh — Tests for
# check-no-test-database-url-fallback.sh.
#
# Creates temporary files to verify that the checker correctly detects
# forbidden `process.env.DATABASE_URL` usage while allowing
# `process.env.TEST_DATABASE_URL`, split-concatenation escape hatches,
# TS/JS comment lines, empty directories, and excluded directories (.git,
# node_modules).
#
# Usage:
#   scripts/tests/test_check_no_test_database_url_fallback.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-test-database-url-fallback.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo.  Intentionally not
# under $REPO/tmp (the check excludes any directory named `tmp`) —
# mktemp -d honours $TMPDIR and defaults to /tmp which is outside the
# exclusion set.
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

# ─── Test 1: Bare process.env.DATABASE_URL in a .ts file should fail ─────
create_test_file "bad.ts" 'const url = process.env.DATABASE_URL;'
assert_fails "Bare process.env.DATABASE_URL in a .ts file is detected"
reset_tmpdir

# ─── Test 2: process.env.TEST_DATABASE_URL should pass ───────────────────
create_test_file "good.ts" 'const url = process.env.TEST_DATABASE_URL;'
assert_passes "process.env.TEST_DATABASE_URL is not flagged"
reset_tmpdir

# ─── Test 3: Split-concatenation escape hatch should pass ─────────────────
# The grep scans for the exact literal `process.env.DATABASE_URL`.  Any form
# that doesn't contain that exact string (including runtime concatenations)
# must not be flagged.
create_test_file "esc.ts" "const key = 'DATA' + 'BASE_URL';"
assert_passes "Split-concatenation escape hatch 'DATA' + 'BASE_URL' is not flagged"
reset_tmpdir

# ─── Test 4: TS comment line should pass ─────────────────────────────────
create_test_file "commented.ts" '// Do not use process.env.DATABASE_URL in tests — use TEST_DATABASE_URL.'
assert_passes "TS // comment line is not flagged"
reset_tmpdir

# ─── Test 5: Empty directory should pass ─────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 6: .git directory contents should be excluded ──────────────────
mkdir -p "$TMPDIR_TEST/.git/objects"
printf 'const url = process.env.DATABASE_URL;\n' > "$TMPDIR_TEST/.git/objects/badfile"
assert_passes ".git directory contents are excluded"
reset_tmpdir

# ─── Test 7: node_modules should be excluded ─────────────────────────────
mkdir -p "$TMPDIR_TEST/node_modules/some-pkg"
printf 'const url = process.env.DATABASE_URL;\n' > "$TMPDIR_TEST/node_modules/some-pkg/index.js"
assert_passes "node_modules directory is excluded"
reset_tmpdir

# ─── Test 8: Multiple violations in one file should fail ─────────────────
create_test_file "multi.ts" 'const a = process.env.DATABASE_URL;
const b = process.env.DATABASE_URL || "fallback";'
assert_fails "Multiple violations in one file are detected"
reset_tmpdir

# ─── Test 9: No self-match on ci.yml step name ───────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden pattern.  If it does, the guard
# fails on its first CI run (see issues #2541/#2542 for the class of
# bug).  Name the CI step after the replacement or category, not the
# literal forbidden string.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-test-database-url-fallback.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
