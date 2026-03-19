#!/usr/bin/env bash
# test_start_worker_max.sh — Tests for start-worker.sh --max-workers flag
#
# Tests argument parsing (unit tests) and limit enforcement (integration
# tests using a temporary git repo).
#
# Usage:
#   scripts/tests/test_start_worker_max.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
START_SCRIPT="$SCRIPT_DIR/start-worker.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_REPO=""
cleanup() {
    if [[ -n "$TEMP_REPO" && -d "$TEMP_REPO" ]]; then
        # Remove all worktrees before deleting the repo
        git -C "$TEMP_REPO" worktree list --porcelain 2>/dev/null \
            | awk '/^worktree / { print $2 }' \
            | while read -r wt; do
                [[ "$wt" == "$TEMP_REPO" ]] && continue
                git -C "$TEMP_REPO" worktree remove "$wt" --force 2>/dev/null || true
            done
        rm -rf "$TEMP_REPO"
    fi
}
trap cleanup EXIT

setup_temp_repo() {
    TEMP_REPO=$(mktemp -d)
    git -C "$TEMP_REPO" init --initial-branch main -q
    git -C "$TEMP_REPO" config user.email "test@example.com"
    git -C "$TEMP_REPO" config user.name "Test"
    touch "$TEMP_REPO/README.md"
    git -C "$TEMP_REPO" add README.md
    git -C "$TEMP_REPO" commit -m "init" -q
    # start-worker.sh fetches origin/main, so create a local "origin"
    git -C "$TEMP_REPO" remote add origin "$TEMP_REPO"
    git -C "$TEMP_REPO" fetch origin main -q 2>/dev/null
    mkdir -p "$TEMP_REPO/worktrees"
    mkdir -p "$TEMP_REPO/.githooks"
    # Copy start-worker.sh into the temp repo
    mkdir -p "$TEMP_REPO/scripts"
    cp "$START_SCRIPT" "$TEMP_REPO/scripts/start-worker.sh"
    chmod +x "$TEMP_REPO/scripts/start-worker.sh"
}

# Create a worker worktree in the temp repo
create_worker() {
    local num="$1"
    local branch="worker-${num}/session-99991231-2359"
    git -C "$TEMP_REPO" worktree add \
        "$TEMP_REPO/worktrees/worker-${num}" -b "$branch" HEAD -q
}

# Run start-worker.sh inside the temp repo and capture outputs
run_in_temp_repo() {
    # start-worker.sh uses `git rev-parse --absolute-git-dir` to find
    # the repo root, so we must run from within the temp repo.
    (cd "$TEMP_REPO" && scripts/start-worker.sh "$@")
}

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# ── Unit Tests: Argument parsing ─────────────────────────────────────────
# These test the argument parser without needing a git repo.

# Test 1: --max-workers rejects non-numeric value
exit_code=0
"$START_SCRIPT" --max-workers abc > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "--max-workers rejects non-numeric value"
else
    fail "--max-workers rejects non-numeric value" "expected exit 1, got $exit_code"
fi

# Test 2: --max-workers rejects zero
exit_code=0
"$START_SCRIPT" --max-workers 0 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "--max-workers rejects zero"
else
    fail "--max-workers rejects zero" "expected exit 1, got $exit_code"
fi

# Test 3: --max-workers rejects negative number
exit_code=0
"$START_SCRIPT" --max-workers -1 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "--max-workers rejects negative number"
else
    fail "--max-workers rejects negative number" "expected exit 1, got $exit_code"
fi

# Test 4: Unknown argument is rejected
exit_code=0
"$START_SCRIPT" --bogus > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "Unknown argument is rejected"
else
    fail "Unknown argument is rejected" "expected exit 1, got $exit_code"
fi

# Test 5: --max-workers without value is rejected
exit_code=0
"$START_SCRIPT" --max-workers > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "--max-workers without value is rejected"
else
    fail "--max-workers without value is rejected" "expected non-zero exit, got 0"
fi

# ── Integration Tests: Limit enforcement ─────────────────────────────────

setup_temp_repo

# Test 6: Refuses when at capacity (2 workers, limit 2)
create_worker 1
create_worker 2

exit_code=0
stderr_output=$(run_in_temp_repo --max-workers 2 2>&1 >/dev/null) || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "Exits non-zero when at capacity (2 active, limit 2)"
else
    fail "Exits non-zero when at capacity" "expected non-zero exit, got 0"
fi

# Test 7: Error message mentions max workers
if echo "$stderr_output" | grep -q "max workers reached"; then
    pass "Error message mentions 'max workers reached'"
else
    fail "Error message mentions 'max workers reached'" "stderr: $stderr_output"
fi

# Test 8: Error message includes the count
if echo "$stderr_output" | grep -q "2 active, limit 2"; then
    pass "Error message includes count details"
else
    fail "Error message includes count details" "stderr: $stderr_output"
fi

# Test 9: Succeeds when under limit
git -C "$TEMP_REPO" worktree remove "$TEMP_REPO/worktrees/worker-2" --force
exit_code=0
new_worktree=$(run_in_temp_repo --max-workers 2 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && -n "$new_worktree" ]]; then
    pass "Succeeds when under limit (1 active, limit 2)"
    # Clean up the new worktree
    git -C "$TEMP_REPO" worktree remove "$new_worktree" --force 2>/dev/null || true
else
    fail "Succeeds when under limit (1 active, limit 2)" "exit code: $exit_code"
fi

# Test 10: No limit (without --max-workers) still works
exit_code=0
no_limit_worktree=$(run_in_temp_repo 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && -n "$no_limit_worktree" ]]; then
    pass "Works without --max-workers (no limit)"
    git -C "$TEMP_REPO" worktree remove "$no_limit_worktree" --force 2>/dev/null || true
else
    fail "Works without --max-workers (no limit)" "exit code: $exit_code"
fi

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
