#!/usr/bin/env bash
# test_check_no_gnu_head.sh — Tests for check-no-gnu-head.sh.
#
# Creates temporary files to verify that the checker correctly detects
# forbidden `head -n -<N>` (GNU-only negative line count) usage while
# allowing comment lines, positive counts, sed replacements, and the
# check script itself to reference the pattern as context.
#
# Usage:
#   scripts/tests/test_check_no_gnu_head.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-gnu-head.sh"
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

# ─── Test 1: bare head -n -1 should fail ─────────────────────────────────
create_test_file "test_foo.sh" 'output=$(head -n -1 "$file")'
assert_fails "bare head -n -1 usage is detected"
reset_tmpdir

# ─── Test 2: head -n -5 (other negative count) should fail ───────────────
create_test_file "test_bar.sh" 'head -n -5 "$logfile" > "$out"'
assert_fails "head -n -5 usage is detected"
reset_tmpdir

# ─── Test 3: comment line should pass ────────────────────────────────────
create_test_file "test_comment.sh" '#!/usr/bin/env bash
# Previously used: head -n -1 to strip last line (GNU-only, broken on macOS)
# Use sed instead.
sed '"'"'$d'"'"' "$file"'
assert_passes "Comment line referencing head -n -1 is allowed"
reset_tmpdir

# ─── Test 4: head -n 100 (positive count) should pass ────────────────────
create_test_file "test_positive.sh" 'head -n 100 "$file"'
assert_passes "head -n 100 (positive count) is not flagged"
reset_tmpdir

# ─── Test 5: sed replacement should pass ─────────────────────────────────
create_test_file "test_sed.sh" 'output=$(sed '"'"'$d'"'"' "$file")'
assert_passes "sed replacement is not flagged"
reset_tmpdir

# ─── Test 6: empty directory should pass ─────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 7: .git directory should be excluded ───────────────────────────
mkdir -p "$TMPDIR_TEST/.git/objects"
printf 'head -n -1 badfile\n' > "$TMPDIR_TEST/.git/objects/badfile"
assert_passes ".git directory contents are excluded"
reset_tmpdir

# ─── Test 8: node_modules should be excluded ─────────────────────────────
mkdir -p "$TMPDIR_TEST/node_modules/some-pkg"
printf 'head -n -1 "$file"\n' > "$TMPDIR_TEST/node_modules/some-pkg/index.sh"
assert_passes "node_modules directory is excluded"
reset_tmpdir

# ─── Test 9: indented comment should pass ────────────────────────────────
create_test_file "test_indented.sh" '    # Legacy: head -n -1 was broken on macOS, replaced with sed.
    sed '"'"'$d'"'"' "$file"'
assert_passes "Indented comment lines are allowed"
reset_tmpdir

# ─── Test 10: mixed file — comment OK, code line fails ───────────────────
create_test_file "test_mixed.sh" '# head -n -1 was the old approach, now replaced
output=$(head -n -1 "$file")'
assert_fails "Mixed file: real usage line is caught even when a comment mentions it"
reset_tmpdir

# ─── Test 11: head -n -0 should fail ─────────────────────────────────────
create_test_file "test_zero.sh" 'head -n -0 "$file"'
assert_fails "head -n -0 (digit after dash) is detected"
reset_tmpdir

# ─── Test 12: head with extra spaces should fail ─────────────────────────
create_test_file "test_spaces.sh" 'head  -n  -1 "$file"'
assert_fails "head with extra spaces between flags is detected"
reset_tmpdir

# ─── Test 13: Non-matching content should pass ───────────────────────────
create_test_file "test_other.sh" 'awk '"'"'NR>1{print prev} {prev=$0}'"'"' "$file"'
assert_passes "awk accumulation alternative does not trigger the check"
reset_tmpdir

# ─── Test 14: self-exclusion — check script and test are excluded ─────────
# Run the check against the scripts/tests/ dir in the actual repo (which
# contains this test file and check-no-gnu-head.sh).  Both files reference
# the pattern as context — they must be self-excluded.
TESTS=$((TESTS + 1))
actual_tests_dir="$SCRIPT_DIR/tests"
if "$CHECK_SCRIPT" "$actual_tests_dir" > /dev/null 2>&1; then
    echo "PASS: Self-exclusion — check script and test file are excluded"
else
    echo "FAIL: Self-exclusion — check ran against scripts/tests/ and failed (check or test file not excluded)"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 15: No self-match on ci.yml step name ──────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden pattern.  If it does, the guard
# fails on its first CI run (see issues #2541/#2542 for the class of
# bug).  Name the CI step after the replacement or category, not the
# literal forbidden string.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-gnu-head.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
