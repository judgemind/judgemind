#!/usr/bin/env bash
# test_check_no_test_leaked_worktrees.sh — Tests for
# check-no-test-leaked-worktrees.sh (issue #4307).
#
# The check guards against synthetic agent worktree directories landing
# under <repo_root>/.claude/worktrees/ during a test run. Real worktrees
# always have hex-only short ids; anything else is a leaked test fixture.
#
# This test fakes a repo root in $TMPDIR and creates synthetic
# .claude/worktrees/agent-* subdirs to exercise both the pass and fail
# paths.
#
# Usage:
#   scripts/tests/test_check_no_test_leaked_worktrees.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-test-leaked-worktrees.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

setup_fake_repo() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
    mkdir -p "$TMPDIR_TEST/.claude/worktrees"
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected exit 0, got non-zero)"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected non-zero exit, got 0)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

# ─── Test 1: fresh repo with no .claude/worktrees passes ──────────────
setup_fake_repo
rm -rf "$TMPDIR_TEST/.claude"
assert_passes "Empty repo (no .claude/worktrees) passes"

# ─── Test 2: empty .claude/worktrees passes ───────────────────────────
setup_fake_repo
assert_passes "Empty .claude/worktrees passes"

# ─── Test 3: real worktree (hex short_id) passes ──────────────────────
setup_fake_repo
mkdir -p "$TMPDIR_TEST/.claude/worktrees/agent-aabbccdd"
assert_passes "Real worktree (agent-aabbccdd) passes"

# ─── Test 4: full uuid form passes ────────────────────────────────────
setup_fake_repo
mkdir -p "$TMPDIR_TEST/.claude/worktrees/agent-aabbccdd-eeff-0011-2233-445566778899"
assert_passes "Full uuid worktree passes"

# ─── Test 5: longer hex short_id passes ───────────────────────────────
setup_fake_repo
# 16+ hex chars (e.g. 16-char short id from longer uuid hash)
mkdir -p "$TMPDIR_TEST/.claude/worktrees/agent-af9e8c95c0d487af1"
assert_passes "Longer hex worktree (16+ chars) passes"

# ─── Test 6: truncated MagicMock leak fails ───────────────────────────
# This is the canonical leak shape from issue #4307 — the
# str(MagicMock()) repr starts with "<MagicMock id='..." and gets
# truncated by [:8] to "<MagicMo".
setup_fake_repo
mkdir -p "$TMPDIR_TEST/.claude/worktrees/agent-<MagicMo"
assert_fails "Truncated MagicMock leak (agent-<MagicMo) fails"

# ─── Test 7: full MagicMock repr leak fails ───────────────────────────
setup_fake_repo
mkdir -p "$TMPDIR_TEST/.claude/worktrees/agent-test-fixture"
assert_fails "Test-fixture-named worktree (agent-test-fixture) fails"

# ─── Test 8: mixed real + leaked detects only the leak ────────────────
setup_fake_repo
mkdir -p "$TMPDIR_TEST/.claude/worktrees/agent-aabbccdd"  # real
mkdir -p "$TMPDIR_TEST/.claude/worktrees/agent-<MagicMo"  # leak
assert_fails "Mixed real+leaked still reports a failure"

# ─── Test 9: non-agent-prefixed dirs are ignored ──────────────────────
setup_fake_repo
mkdir -p "$TMPDIR_TEST/.claude/worktrees/something-else"
mkdir -p "$TMPDIR_TEST/.claude/worktrees/.lockfile"
assert_passes "Non-agent-prefixed directories are ignored"

# ─── Test 10: stderr output identifies the leaked path ────────────────
setup_fake_repo
mkdir -p "$TMPDIR_TEST/.claude/worktrees/agent-<MagicMo"
TESTS=$((TESTS + 1))
stderr_out=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 1>/dev/null || true)
if echo "$stderr_out" | grep -F "agent-<MagicMo" > /dev/null; then
    echo "PASS: Failure message names the leaked path"
else
    echo "FAIL: Failure message did not name the leaked path. stderr: $stderr_out"
    FAILURES=$((FAILURES + 1))
fi

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
