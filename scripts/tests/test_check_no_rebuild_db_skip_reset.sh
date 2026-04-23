#!/usr/bin/env bash
# test_check_no_rebuild_db_skip_reset.sh — Tests for
# check-no-rebuild-db-skip-reset.sh.
#
# Creates temporary files to verify that the checker correctly detects
# forbidden `rebuild_db.py ... --skip-reset` usage while allowing
# comments, docs/investigations/ post-mortems, the valid shell wrapper
# (`rebuild_db.sh --skip-reset`), and the check script itself to
# reference the pattern as context.
#
# Usage:
#   scripts/tests/test_check_no_rebuild_db_skip_reset.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-rebuild-db-skip-reset.sh"
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

# ─── Test 1: Bare Markdown reference should fail ─────────────────────────
create_test_file "docs/bad.md" 'Run: rebuild_db.py --county X --skip-reset'
assert_fails "Bare Markdown reference to rebuild_db.py --skip-reset is detected"
reset_tmpdir

# ─── Test 2: ecs-run-task.sh wrapper form should fail ────────────────────
create_test_file "docs/ecs.md" \
    'scripts/ecs-run-task.sh scripts/rebuild_db.py -- --county "Orange" --skip-reset'
assert_fails "ecs-run-task.sh wrapper form is detected"
reset_tmpdir

# ─── Test 3: Table row in markdown should fail ───────────────────────────
create_test_file "docs/table.md" \
    '| Full rebuild | `rebuild_db.py` (no `--skip-reset`) | truncates first |'
assert_fails "Table-row reference with backticks is detected"
reset_tmpdir

# ─── Test 4: Shell wrapper `rebuild_db.sh --skip-reset` should pass ──────
# The shell wrapper is the valid caller of --skip-reset.  The check must
# NOT flag it — this is the critical distinguishing behavior vs the .py
# script.
create_test_file "docs/wrapper.md" 'Use: scripts/rebuild_db.sh --skip-reset'
assert_passes "Valid shell wrapper rebuild_db.sh --skip-reset is not flagged"
reset_tmpdir

# ─── Test 5: Python script without --skip-reset should pass ──────────────
create_test_file "docs/rebuild.md" 'Run: scripts/rebuild_db.py --county X'
assert_passes "rebuild_db.py without --skip-reset passes"
reset_tmpdir

# ─── Test 6: Python script with --reset (opt-in) should pass ─────────────
create_test_file "docs/reset.md" 'Run: scripts/rebuild_db.py --county X --reset'
assert_passes "rebuild_db.py --reset (valid opt-in flag) passes"
reset_tmpdir

# ─── Test 7: Shell comment lines should pass ─────────────────────────────
create_test_file "cleanup.sh" '#!/usr/bin/env bash
# Previously agents were told to run `rebuild_db.py --skip-reset` which
# does not exist (see issue #2591).  Drop the flag.
scripts/rebuild_db.py --county X'
assert_passes "Shell comments referencing the pattern as context are allowed"
reset_tmpdir

# ─── Test 8: YAML comment lines should pass ──────────────────────────────
create_test_file "workflow.yml" 'jobs:
  build:
    steps:
      # Do not run: rebuild_db.py --skip-reset (see #2591).
      - run: scripts/rebuild_db.py --county X'
assert_passes "YAML comments referencing the pattern as context are allowed"
reset_tmpdir

# ─── Test 9: docs/investigations/*.md should pass ────────────────────────
create_test_file "docs/investigations/rebuild-flag-2026-04.md" \
    '# rebuild_db.py --skip-reset incident

Prior docs instructed agents to pass `--skip-reset` which does not exist
on the Python script.  This investigation records the cleanup.'
assert_passes "docs/investigations/*.md post-mortems are allowed to reference the pattern"
reset_tmpdir

# ─── Test 10: Empty directory should pass ────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test 11: Non-matching shell content should pass ─────────────────────
create_test_file "good.sh" 'scripts/rebuild_db.py --county X --concurrency 16'
assert_passes "Shell script using rebuild_db.py with other flags passes"
reset_tmpdir

# ─── Test 12: .git directory should be excluded ──────────────────────────
mkdir -p "$TMPDIR_TEST/.git/objects"
printf 'rebuild_db.py --skip-reset\n' > "$TMPDIR_TEST/.git/objects/badfile"
assert_passes ".git directory contents are excluded"
reset_tmpdir

# ─── Test 13: node_modules should be excluded ────────────────────────────
mkdir -p "$TMPDIR_TEST/node_modules/some-pkg"
printf 'rebuild_db.py --skip-reset\n' > "$TMPDIR_TEST/node_modules/some-pkg/index.js"
assert_passes "node_modules directory is excluded"
reset_tmpdir

# ─── Test 14: Indented comment with the pattern should pass ──────────────
create_test_file "indented.sh" '    # Legacy: rebuild_db.py --skip-reset was broken.
    scripts/rebuild_db.py --county X'
assert_passes "Indented comment lines are allowed"
reset_tmpdir

# ─── Test 15: Mixed file — comment OK, code line fails ───────────────────
create_test_file "mixed.md" '# History

The following command does not work:

    scripts/rebuild_db.py --skip-reset --county X'
assert_fails "Mixed file: real usage line is caught even when a heading mentions it"
reset_tmpdir

# ─── Test 16: Multiple violations in one file are detected ───────────────
create_test_file "multi.md" 'First: rebuild_db.py --county A --skip-reset
Second: rebuild_db.py --county B --skip-reset'
assert_fails "Multiple violations in one file are detected"
reset_tmpdir

# ─── Test 17: Similar-but-not-matching token should pass ─────────────────
# `rebuild_db_py_skip_reset` as a variable name or filename is not the
# invocation we care about and should not trigger the check.
create_test_file "lookalike.sh" 'VAR=rebuild_db_py_skip_reset
echo "$VAR"'
assert_passes "Underscore-joined lookalike identifier does not trigger the check"
reset_tmpdir

# ─── Test 18: --skip-reset on a different line should pass ───────────────
# The pattern requires rebuild_db.py and --skip-reset on the same line.
# A sentence break between them (distinct lines) must not match.
create_test_file "multiline.md" 'Run rebuild_db.py first.
Then consider --skip-reset on the shell wrapper.'
assert_passes "rebuild_db.py and --skip-reset on different lines do not match"
reset_tmpdir

# ─── Test 19: No self-match on ci.yml step name ──────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden pattern.  If it does, the guard
# fails on its first CI run (see issues #2541/#2542 for the class of
# bug).  Name the CI step after the replacement or category, not the
# literal forbidden string.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-rebuild-db-skip-reset.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
