#!/usr/bin/env bash
# test_sweep_stale_worktrees.sh — Tests for sweep_stale_worktrees.sh
#
# Verifies that the sweep command correctly identifies and removes stale
# metadata entries while leaving live worktrees untouched.
#
# Usage:
#   scripts/tests/test_sweep_stale_worktrees.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SWEEP_SCRIPT="$SCRIPT_DIR/sweep_stale_worktrees.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
cleanup() {
    set +e
    for d in "${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}"; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    echo "$dir"
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

# Make a fake repo with the sweep script wired up. Returns the repo path.
# Structure:
#   $repo/.git/                      — real .git directory (from init)
#   $repo/CLAUDE.md                  — marker file for find_repo_root
#   $repo/scripts/sweep_stale_worktrees.sh  — copy of the script under test
make_repo() {
    local repo
    repo=$(make_temp_dir)
    git -C "$repo" init --initial-branch main -q
    git -C "$repo" config user.email "test@example.com"
    git -C "$repo" config user.name "Test"
    touch "$repo/CLAUDE.md"
    git -C "$repo" add CLAUDE.md
    git -C "$repo" commit -m "init" -q
    mkdir -p "$repo/scripts"
    cp "$SWEEP_SCRIPT" "$repo/scripts/sweep_stale_worktrees.sh"
    chmod +x "$repo/scripts/sweep_stale_worktrees.sh"
    echo "$repo"
}

# Create a fake metadata entry. Arguments:
#   $1 repo path
#   $2 agent name (e.g. agent-abcd1234)
#   $3 "live" or "orphan" — controls whether the on-disk worktree dir exists
#   $4 "locked" (optional) — place a `locked` marker in the metadata dir
make_metadata_entry() {
    local repo="$1"
    local name="$2"
    local kind="$3"
    local locked="${4:-}"

    local worktree_dir="$repo/.claude/worktrees/$name"
    local metadata_dir="$repo/.git/worktrees/$name"

    mkdir -p "$metadata_dir"
    # The `gitdir` pointer points to $worktree_dir/.git — the inner .git file.
    # sweep uses dirname() on this to get the worktree dir.
    echo "$worktree_dir/.git" > "$metadata_dir/gitdir"
    touch "$metadata_dir/HEAD"
    touch "$metadata_dir/commondir"

    if [[ -n "$locked" ]]; then
        echo "locked" > "$metadata_dir/locked"
    fi

    if [[ "$kind" == "live" ]]; then
        mkdir -p "$worktree_dir"
        # Pretend the worktree has its inner .git pointer file
        echo "gitdir: $metadata_dir" > "$worktree_dir/.git"
    fi
    # For "orphan", we deliberately do NOT create $worktree_dir — that is
    # the exact state we want the sweep to detect.
}

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: no metadata at all — sweep succeeds and reports 0.
test_empty_metadata_dir() {
    local repo
    repo=$(make_repo)

    local output exit_code=0
    output=$("$repo/scripts/sweep_stale_worktrees.sh" 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        fail "empty metadata dir — sweep succeeds" "exit code $exit_code; output: $output"
        return
    fi
    if echo "$output" | grep -q "Cleaned up 0 stale worktree entries"; then
        pass "empty metadata dir — sweep succeeds and reports 0"
    else
        fail "empty metadata dir — sweep reports count" "output: $output"
    fi
}

# Test 2: one live + one orphan entry — only the orphan is removed.
test_live_and_orphan() {
    local repo
    repo=$(make_repo)
    make_metadata_entry "$repo" "agent-aaaa1111" "live"
    make_metadata_entry "$repo" "agent-bbbb2222" "orphan"

    local output exit_code=0
    output=$("$repo/scripts/sweep_stale_worktrees.sh" 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        fail "live + orphan — sweep succeeds" "exit code $exit_code; output: $output"
        return
    fi

    # Live metadata must still exist
    if [[ ! -d "$repo/.git/worktrees/agent-aaaa1111" ]]; then
        fail "live + orphan — live metadata preserved" "metadata for live worktree was removed"
        return
    fi
    # Orphan metadata must be gone
    if [[ -d "$repo/.git/worktrees/agent-bbbb2222" ]]; then
        fail "live + orphan — orphan metadata removed" "metadata for orphan worktree still exists"
        return
    fi
    # Summary line must say 1
    if ! echo "$output" | grep -q "Cleaned up 1 stale worktree entries"; then
        fail "live + orphan — summary count" "output: $output"
        return
    fi

    pass "live + orphan — orphan removed, live preserved, summary reports 1"
}

# Test 3: locked live worktree — NOT touched even though locked.
# Policy: we never touch a worktree whose on-disk dir still exists,
# regardless of `locked` state.
test_locked_live_is_preserved() {
    local repo
    repo=$(make_repo)
    make_metadata_entry "$repo" "agent-cccc3333" "live" "locked"

    local output exit_code=0
    output=$("$repo/scripts/sweep_stale_worktrees.sh" 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        fail "locked live — sweep succeeds" "exit code $exit_code; output: $output"
        return
    fi

    if [[ ! -d "$repo/.git/worktrees/agent-cccc3333" ]]; then
        fail "locked live — metadata preserved" "locked live worktree's metadata was removed"
        return
    fi
    if ! echo "$output" | grep -q "Cleaned up 0 stale worktree entries"; then
        fail "locked live — summary count" "output: $output"
        return
    fi

    pass "locked live — preserved, summary reports 0"
}

# Test 4: locked orphan worktree — STILL removed, matching the #2388
# policy (missing dir means definitively dead, ignore locked).
test_locked_orphan_is_removed() {
    local repo
    repo=$(make_repo)
    make_metadata_entry "$repo" "agent-dddd4444" "orphan" "locked"

    local output exit_code=0
    output=$("$repo/scripts/sweep_stale_worktrees.sh" 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        fail "locked orphan — sweep succeeds" "exit code $exit_code; output: $output"
        return
    fi

    if [[ -d "$repo/.git/worktrees/agent-dddd4444" ]]; then
        fail "locked orphan — metadata removed" "locked orphan metadata still exists"
        return
    fi
    if ! echo "$output" | grep -q "Cleaned up 1 stale worktree entries"; then
        fail "locked orphan — summary count" "output: $output"
        return
    fi

    pass "locked orphan — removed despite locked, summary reports 1"
}

# Test 5: multiple orphans — all removed, count matches.
test_many_orphans() {
    local repo
    repo=$(make_repo)
    make_metadata_entry "$repo" "agent-1111aaaa" "orphan"
    make_metadata_entry "$repo" "agent-2222bbbb" "orphan"
    make_metadata_entry "$repo" "agent-3333cccc" "orphan"
    make_metadata_entry "$repo" "agent-4444dddd" "live"

    local output exit_code=0
    output=$("$repo/scripts/sweep_stale_worktrees.sh" 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        fail "many orphans — sweep succeeds" "exit code $exit_code; output: $output"
        return
    fi
    if ! echo "$output" | grep -q "Cleaned up 3 stale worktree entries"; then
        fail "many orphans — summary count" "output: $output"
        return
    fi
    if [[ ! -d "$repo/.git/worktrees/agent-4444dddd" ]]; then
        fail "many orphans — live preserved" "live metadata was removed"
        return
    fi
    local remaining
    remaining=$(find "$repo/.git/worktrees" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')
    if [[ "$remaining" != "1" ]]; then
        fail "many orphans — only live remaining" "expected 1 remaining entry, got $remaining"
        return
    fi
    pass "many orphans — 3 removed, 1 live preserved"
}

# Test 6: --dry-run does NOT remove, but still reports the count.
test_dry_run() {
    local repo
    repo=$(make_repo)
    make_metadata_entry "$repo" "agent-eeee5555" "orphan"
    make_metadata_entry "$repo" "agent-ffff6666" "orphan"

    local output exit_code=0
    output=$("$repo/scripts/sweep_stale_worktrees.sh" --dry-run 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        fail "dry-run — sweep succeeds" "exit code $exit_code; output: $output"
        return
    fi
    if [[ ! -d "$repo/.git/worktrees/agent-eeee5555" ]]; then
        fail "dry-run — metadata preserved" "metadata was removed in dry-run mode"
        return
    fi
    if [[ ! -d "$repo/.git/worktrees/agent-ffff6666" ]]; then
        fail "dry-run — metadata preserved" "metadata was removed in dry-run mode"
        return
    fi
    if ! echo "$output" | grep -q "Cleaned up 2 stale worktree entries (dry-run)"; then
        fail "dry-run — summary count" "output: $output"
        return
    fi
    pass "dry-run — no removal, summary reports 2 with (dry-run) tag"
}

# Test 7: corrupt entry with no gitdir pointer — treated as stale, removed.
test_corrupt_entry_no_gitdir() {
    local repo
    repo=$(make_repo)
    mkdir -p "$repo/.git/worktrees/agent-corrupt/"
    # No gitdir file at all

    local output exit_code=0
    output=$("$repo/scripts/sweep_stale_worktrees.sh" 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        fail "corrupt entry — sweep succeeds" "exit code $exit_code; output: $output"
        return
    fi
    if [[ -d "$repo/.git/worktrees/agent-corrupt" ]]; then
        fail "corrupt entry — removed" "corrupt metadata still exists"
        return
    fi
    if ! echo "$output" | grep -q "Cleaned up 1 stale worktree entries"; then
        fail "corrupt entry — summary count" "output: $output"
        return
    fi
    pass "corrupt entry (no gitdir) — treated as stale and removed"
}

# Test 8: --quiet suppresses per-entry logs but keeps the summary line.
test_quiet_mode() {
    local repo
    repo=$(make_repo)
    make_metadata_entry "$repo" "agent-7777eeee" "orphan"

    local output exit_code=0
    output=$("$repo/scripts/sweep_stale_worktrees.sh" --quiet 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        fail "quiet mode — sweep succeeds" "exit code $exit_code; output: $output"
        return
    fi
    # Should not contain the per-entry log line
    if echo "$output" | grep -q "Stale"; then
        fail "quiet mode — per-entry logs suppressed" "log line leaked through: $output"
        return
    fi
    # Must still contain the summary
    if ! echo "$output" | grep -q "Cleaned up 1 stale worktree entries"; then
        fail "quiet mode — summary still emitted" "output: $output"
        return
    fi
    pass "quiet mode — per-entry logs hidden, summary still emitted"
}

# Test 9: script works when invoked from a copy placed under scripts/ in a
# worktree-style setup (CLAUDE.md + .git file, not directory). It must walk
# up past the worktree to find the real repo root.
test_invoke_from_worktree_scripts_dir() {
    local repo
    repo=$(make_repo)

    local worktree="$repo/.claude/worktrees/agent-fake9999"
    mkdir -p "$worktree/scripts"
    touch "$worktree/CLAUDE.md"
    # Worktrees have .git as a FILE, not a directory
    echo "gitdir: $repo/.git/worktrees/agent-fake9999" > "$worktree/.git"
    # Place a copy of the sweep script in the worktree's scripts dir
    cp "$SWEEP_SCRIPT" "$worktree/scripts/sweep_stale_worktrees.sh"
    chmod +x "$worktree/scripts/sweep_stale_worktrees.sh"

    # Make a real metadata entry + matching on-disk worktree so it is "live"
    make_metadata_entry "$repo" "agent-8888ffff" "live"
    # Also make an orphan metadata entry to sweep
    make_metadata_entry "$repo" "agent-9999aaaa" "orphan"

    # Run the sweep via the worktree copy
    local output exit_code=0
    output=$("$worktree/scripts/sweep_stale_worktrees.sh" 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        fail "invoke-from-worktree — sweep succeeds" "exit code $exit_code; output: $output"
        return
    fi
    if [[ ! -d "$repo/.git/worktrees/agent-8888ffff" ]]; then
        fail "invoke-from-worktree — live preserved" "live metadata removed"
        return
    fi
    if [[ -d "$repo/.git/worktrees/agent-9999aaaa" ]]; then
        fail "invoke-from-worktree — orphan removed" "orphan metadata still exists"
        return
    fi
    pass "invoke-from-worktree — walks up past worktree to main repo root"
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_empty_metadata_dir
test_live_and_orphan
test_locked_live_is_preserved
test_locked_orphan_is_removed
test_many_orphans
test_dry_run
test_corrupt_entry_no_gitdir
test_quiet_mode
test_invoke_from_worktree_scripts_dir

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
