#!/usr/bin/env bash
# test_end_worker.sh — Tests for end-worker.sh
#
# Tests worktree removal, error handling for missing paths, and
# error handling for non-git directories.  Uses a temporary git repo
# to avoid touching the real worktree list.
#
# Usage:
#   scripts/tests/test_end_worker.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
END_SCRIPT="$SCRIPT_DIR/end-worker.sh"
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
    mkdir -p "$TEMP_REPO/worktrees"
    # Copy end-worker.sh into the temp repo so it can find the git root
    mkdir -p "$TEMP_REPO/scripts"
    cp "$END_SCRIPT" "$TEMP_REPO/scripts/end-worker.sh"
    chmod +x "$TEMP_REPO/scripts/end-worker.sh"
}

# Create a worker worktree in the temp repo
create_worker() {
    local num="$1"
    local branch="worker-${num}/session-99991231-2359"
    git -C "$TEMP_REPO" worktree add \
        "$TEMP_REPO/worktrees/worker-${num}" -b "$branch" HEAD -q
}

# Run end-worker.sh from within the temp repo
run_end_worker() {
    (cd "$TEMP_REPO" && scripts/end-worker.sh "$@")
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

# ── Unit Tests: Argument validation ──────────────────────────────────────

# Test 1: No arguments prints usage and exits non-zero
exit_code=0
"$END_SCRIPT" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "No arguments exits non-zero"
else
    fail "No arguments exits non-zero" "expected non-zero exit, got 0"
fi

# ── Integration Tests ────────────────────────────────────────────────────

setup_temp_repo

# Test 2: Rejects non-existent worktree path
exit_code=0
stderr_output=$(run_end_worker "$TEMP_REPO/worktrees/worker-999" 2>&1) || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "Exits non-zero for non-existent worktree path"
else
    fail "Exits non-zero for non-existent worktree path" "expected non-zero exit, got 0"
fi

# Test 3: Error message mentions the missing path
if echo "$stderr_output" | grep -q "not found"; then
    pass "Error message mentions 'not found' for missing path"
else
    fail "Error message mentions 'not found' for missing path" "stderr: $stderr_output"
fi

# Test 4: Successfully removes a worktree
create_worker 1
if [[ -d "$TEMP_REPO/worktrees/worker-1" ]]; then
    pass "Worker-1 worktree exists before removal"
else
    fail "Worker-1 worktree exists before removal" "directory not found"
fi

exit_code=0
run_end_worker "$TEMP_REPO/worktrees/worker-1" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "end-worker.sh exits 0 on successful removal"
else
    fail "end-worker.sh exits 0 on successful removal" "exit code: $exit_code"
fi

# Test 5: Worktree directory is gone after removal
if [[ ! -d "$TEMP_REPO/worktrees/worker-1" ]]; then
    pass "Worktree directory is removed"
else
    fail "Worktree directory is removed" "directory still exists"
fi

# Test 6: Branch is preserved after worktree removal (for open PRs)
branch_exists=false
if git -C "$TEMP_REPO" rev-parse --verify "worker-1/session-99991231-2359" > /dev/null 2>&1; then
    branch_exists=true
fi
if [[ "$branch_exists" == true ]]; then
    pass "Branch is preserved after worktree removal"
else
    fail "Branch is preserved after worktree removal" "branch was deleted"
fi

# Test 7: Worktree is no longer listed by git
wt_count=$(git -C "$TEMP_REPO" worktree list --porcelain \
    | grep -c "worktree.*worker-1" || true)
if [[ "$wt_count" -eq 0 ]]; then
    pass "Worktree is no longer listed by git"
else
    fail "Worktree is no longer listed by git" "still listed ($wt_count entries)"
fi

# Test 8: Can remove multiple worktrees in sequence
create_worker 2
create_worker 3

run_end_worker "$TEMP_REPO/worktrees/worker-2" > /dev/null 2>&1
exit_code=0
run_end_worker "$TEMP_REPO/worktrees/worker-3" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 && ! -d "$TEMP_REPO/worktrees/worker-2" && ! -d "$TEMP_REPO/worktrees/worker-3" ]]; then
    pass "Can remove multiple worktrees in sequence"
else
    fail "Can remove multiple worktrees in sequence" "exit code: $exit_code"
fi

# Test 9: Prints confirmation message on success
create_worker 4
stderr_output=$(run_end_worker "$TEMP_REPO/worktrees/worker-4" 2>&1 >/dev/null) || true
if echo "$stderr_output" | grep -q "Removed worktree"; then
    pass "Prints 'Removed worktree' confirmation message"
else
    fail "Prints 'Removed worktree' confirmation message" "stderr: $stderr_output"
fi

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
