#!/usr/bin/env bash
# test_agent_lock.sh — Tests for the agent lock file mechanism (#1260, #1254)
#
# Tests:
#   - start-worker.sh creates .agent-lock in new worktrees
#   - end-worker.sh refuses to destroy a worktree with a fresh lock
#   - end-worker.sh --force overrides the lock check
#   - end-worker.sh ignores stale locks (> 30 min)
#   - start-worker.sh skips stale worktrees with fresh locks during cleanup
#   - refresh-agent-lock.sh creates/updates the lock file
#   - start-worker.sh preserves unregistered dirs with fresh locks (#1254)
#   - start-worker.sh skips worker numbers with fresh-locked dirs (#1254)
#   - start-worker.sh reclaims worker numbers after stale/missing locks (#1254)
#
# Usage:
#   scripts/tests/test_agent_lock.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
START_SCRIPT="$SCRIPT_DIR/start-worker.sh"
END_SCRIPT="$SCRIPT_DIR/end-worker.sh"
REFRESH_SCRIPT="$SCRIPT_DIR/refresh-agent-lock.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_REPO=""
cleanup() {
    if [[ -n "$TEMP_REPO" && -d "$TEMP_REPO" ]]; then
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
    git -C "$TEMP_REPO" remote add origin "$TEMP_REPO"
    git -C "$TEMP_REPO" fetch origin main -q 2>/dev/null
    mkdir -p "$TEMP_REPO/worktrees"
    mkdir -p "$TEMP_REPO/.githooks"
    mkdir -p "$TEMP_REPO/scripts"
    cp "$START_SCRIPT" "$TEMP_REPO/scripts/start-worker.sh"
    cp "$END_SCRIPT" "$TEMP_REPO/scripts/end-worker.sh"
    cp "$REFRESH_SCRIPT" "$TEMP_REPO/scripts/refresh-agent-lock.sh"
    chmod +x "$TEMP_REPO/scripts/start-worker.sh"
    chmod +x "$TEMP_REPO/scripts/end-worker.sh"
    chmod +x "$TEMP_REPO/scripts/refresh-agent-lock.sh"
}

create_worker() {
    local num="$1"
    local branch="worker-${num}/session-99991231-2359"
    git -C "$TEMP_REPO" worktree add \
        "$TEMP_REPO/worktrees/worker-${num}" -b "$branch" HEAD -q
}

run_start() {
    (cd "$TEMP_REPO" && scripts/start-worker.sh "$@")
}

run_end() {
    (cd "$TEMP_REPO" && scripts/end-worker.sh "$@")
}

run_refresh() {
    (cd "$TEMP_REPO" && scripts/refresh-agent-lock.sh "$@")
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

# ── Tests ──────────────────────────────────────────────────────────────────

setup_temp_repo

# ── Test 1: start-worker.sh creates .agent-lock ──────────────────────────
new_wt=$(run_start 2>/dev/null | tail -1) || true
if [[ -f "$new_wt/.agent-lock" ]]; then
    pass "start-worker.sh creates .agent-lock in new worktree"
else
    fail "start-worker.sh creates .agent-lock in new worktree" "file not found in $new_wt"
fi

# ── Test 2: .agent-lock contains a valid ISO timestamp ───────────────────
lock_content=$(cat "$new_wt/.agent-lock" 2>/dev/null || echo "")
if [[ "$lock_content" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T ]]; then
    pass ".agent-lock contains ISO timestamp"
else
    fail ".agent-lock contains ISO timestamp" "content: '$lock_content'"
fi

# Clean up the worktree created by start-worker (use --force since lock is fresh)
git -C "$TEMP_REPO" worktree remove "$new_wt" --force 2>/dev/null || true

# ── Test 3: end-worker.sh refuses to destroy worktree with fresh lock ────
create_worker 10
date -u +%Y-%m-%dT%H:%M:%SZ > "$TEMP_REPO/worktrees/worker-10/.agent-lock"

exit_code=0
stderr_output=$(run_end "$TEMP_REPO/worktrees/worker-10" 2>&1) || exit_code=$?
if [[ "$exit_code" -ne 0 ]]; then
    pass "end-worker.sh refuses to destroy worktree with fresh lock"
else
    fail "end-worker.sh refuses to destroy worktree with fresh lock" "expected non-zero exit, got 0"
fi

# ── Test 4: Error message mentions the lock ──────────────────────────────
if echo "$stderr_output" | grep -q "fresh agent lock"; then
    pass "Error message mentions 'fresh agent lock'"
else
    fail "Error message mentions 'fresh agent lock'" "stderr: $stderr_output"
fi

# ── Test 5: Worktree still exists after refused removal ──────────────────
if [[ -d "$TEMP_REPO/worktrees/worker-10" ]]; then
    pass "Worktree still exists after refused removal"
else
    fail "Worktree still exists after refused removal" "directory was removed despite lock"
fi

# ── Test 6: end-worker.sh --force overrides the lock ────────────────────
exit_code=0
run_end "$TEMP_REPO/worktrees/worker-10" --force > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 && ! -d "$TEMP_REPO/worktrees/worker-10" ]]; then
    pass "end-worker.sh --force overrides fresh lock"
else
    fail "end-worker.sh --force overrides fresh lock" "exit: $exit_code, dir exists: $(test -d "$TEMP_REPO/worktrees/worker-10" && echo yes || echo no)"
fi

# ── Test 7: end-worker.sh removes worktree with stale lock (> 30 min) ───
create_worker 11
date -u +%Y-%m-%dT%H:%M:%SZ > "$TEMP_REPO/worktrees/worker-11/.agent-lock"
# Backdate the lock file by 31 minutes
touch -t "$(date -v-31M +%Y%m%d%H%M.%S 2>/dev/null || date -d '31 minutes ago' +%Y%m%d%H%M.%S)" "$TEMP_REPO/worktrees/worker-11/.agent-lock"

exit_code=0
run_end "$TEMP_REPO/worktrees/worker-11" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 && ! -d "$TEMP_REPO/worktrees/worker-11" ]]; then
    pass "end-worker.sh removes worktree with stale lock (> 30 min)"
else
    fail "end-worker.sh removes worktree with stale lock (> 30 min)" "exit: $exit_code"
fi

# ── Test 8: end-worker.sh removes worktree with no lock file ────────────
create_worker 12
exit_code=0
run_end "$TEMP_REPO/worktrees/worker-12" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 0 && ! -d "$TEMP_REPO/worktrees/worker-12" ]]; then
    pass "end-worker.sh removes worktree without lock file"
else
    fail "end-worker.sh removes worktree without lock file" "exit: $exit_code"
fi

# ── Test 9: refresh-agent-lock.sh creates lock if missing ────────────────
create_worker 13
# Ensure no lock exists
rm -f "$TEMP_REPO/worktrees/worker-13/.agent-lock"

run_refresh "$TEMP_REPO/worktrees/worker-13" > /dev/null 2>&1
if [[ -f "$TEMP_REPO/worktrees/worker-13/.agent-lock" ]]; then
    pass "refresh-agent-lock.sh creates lock if missing"
else
    fail "refresh-agent-lock.sh creates lock if missing"
fi

# ── Test 10: refresh-agent-lock.sh updates existing lock ─────────────────
# Backdate the lock, then refresh, then check it's fresh
touch -t "$(date -v-31M +%Y%m%d%H%M.%S 2>/dev/null || date -d '31 minutes ago' +%Y%m%d%H%M.%S)" "$TEMP_REPO/worktrees/worker-13/.agent-lock"

before_mtime=$(stat -f %m "$TEMP_REPO/worktrees/worker-13/.agent-lock" 2>/dev/null || stat -c %Y "$TEMP_REPO/worktrees/worker-13/.agent-lock")
sleep 1
run_refresh "$TEMP_REPO/worktrees/worker-13" > /dev/null 2>&1
after_mtime=$(stat -f %m "$TEMP_REPO/worktrees/worker-13/.agent-lock" 2>/dev/null || stat -c %Y "$TEMP_REPO/worktrees/worker-13/.agent-lock")

if [[ "$after_mtime" -gt "$before_mtime" ]]; then
    pass "refresh-agent-lock.sh updates lock modification time"
else
    fail "refresh-agent-lock.sh updates lock modification time" "before=$before_mtime after=$after_mtime"
fi

# Clean up worker 13
git -C "$TEMP_REPO" worktree remove "$TEMP_REPO/worktrees/worker-13" --force 2>/dev/null || true

# ── Test 11: start-worker.sh skips stale worktree with fresh lock ────────
# Create a worktree on a branch that is already merged (so it would be
# considered stale) but give it a fresh lock — it should be preserved.
create_worker 14
date -u +%Y-%m-%dT%H:%M:%SZ > "$TEMP_REPO/worktrees/worker-14/.agent-lock"
# Worker 14's branch (worker-14/session-99991231-2359) is based on HEAD which
# IS main, so git branch --merged will include it → stale by rule 1.
# But the fresh lock should protect it.

# Attempt to start a new worker — this triggers the stale cleanup pass
new_wt2=$(run_start 2>/dev/null | tail -1) || true
if [[ -d "$TEMP_REPO/worktrees/worker-14" ]]; then
    pass "start-worker.sh skips stale worktree with fresh lock"
else
    fail "start-worker.sh skips stale worktree with fresh lock" "worker-14 was destroyed"
fi
# Clean up
git -C "$TEMP_REPO" worktree remove "$new_wt2" --force 2>/dev/null || true
git -C "$TEMP_REPO" worktree remove "$TEMP_REPO/worktrees/worker-14" --force 2>/dev/null || true

# ── Test 12: start-worker.sh skips unregistered dir with fresh lock (#1254) ──
# Create an unregistered directory (not a git worktree) with a fresh lock.
# start-worker.sh Step 1d should NOT remove it, and Step 2 should skip its
# worker number.
mkdir -p "$TEMP_REPO/worktrees/worker-1"
date -u +%Y-%m-%dT%H:%M:%SZ > "$TEMP_REPO/worktrees/worker-1/.agent-lock"

new_wt3=$(run_start 2>/dev/null | tail -1) || true
if [[ -d "$TEMP_REPO/worktrees/worker-1" ]]; then
    pass "start-worker.sh preserves unregistered dir with fresh lock (Step 1d) (#1254)"
else
    fail "start-worker.sh preserves unregistered dir with fresh lock (Step 1d) (#1254)" "worker-1 directory was removed"
fi

# Verify the new worktree was assigned a DIFFERENT number (not worker-1)
new_wt3_num=$(basename "$new_wt3" | sed 's/worker-//')
if [[ "$new_wt3_num" != "1" ]]; then
    pass "start-worker.sh assigns different worker number when locked dir exists (Step 2) (#1254)"
else
    fail "start-worker.sh assigns different worker number when locked dir exists (Step 2) (#1254)" "got worker-1, expected a different number"
fi

# Clean up
git -C "$TEMP_REPO" worktree remove "$new_wt3" --force 2>/dev/null || true
rm -rf "$TEMP_REPO/worktrees/worker-1"

# ── Test 13: start-worker.sh removes unregistered dir with stale lock (#1254) ──
# An unregistered directory with a stale lock (> 30 min) should be cleaned up.
mkdir -p "$TEMP_REPO/worktrees/worker-1"
date -u +%Y-%m-%dT%H:%M:%SZ > "$TEMP_REPO/worktrees/worker-1/.agent-lock"
touch -t "$(date -v-31M +%Y%m%d%H%M.%S 2>/dev/null || date -d '31 minutes ago' +%Y%m%d%H%M.%S)" "$TEMP_REPO/worktrees/worker-1/.agent-lock"

new_wt4=$(run_start 2>/dev/null | tail -1) || true
new_wt4_num=$(basename "$new_wt4" | sed 's/worker-//')
if [[ "$new_wt4_num" == "1" ]]; then
    pass "start-worker.sh reclaims worker number after stale lock removal (#1254)"
else
    fail "start-worker.sh reclaims worker number after stale lock removal (#1254)" "got worker-$new_wt4_num, expected worker-1"
fi
# Clean up
git -C "$TEMP_REPO" worktree remove "$new_wt4" --force 2>/dev/null || true

# ── Test 14: start-worker.sh removes unregistered dir without lock (#1254) ──
# An unregistered directory with no lock at all should be cleaned up normally.
mkdir -p "$TEMP_REPO/worktrees/worker-1"

new_wt5=$(run_start 2>/dev/null | tail -1) || true
new_wt5_num=$(basename "$new_wt5" | sed 's/worker-//')
if [[ "$new_wt5_num" == "1" ]]; then
    pass "start-worker.sh reclaims worker number from unregistered dir without lock (#1254)"
else
    fail "start-worker.sh reclaims worker number from unregistered dir without lock (#1254)" "got worker-$new_wt5_num, expected worker-1"
fi
# Clean up
git -C "$TEMP_REPO" worktree remove "$new_wt5" --force 2>/dev/null || true

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
