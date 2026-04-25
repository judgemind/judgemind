#!/usr/bin/env bash
# test_write_claude_file.sh — Tests for write-claude-file.sh permission preservation
#
# Usage: scripts/tests/test_write_claude_file.sh
#
# Exits 0 if all tests pass, 1 if any test fails.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$SCRIPT_DIR/write-claude-file.sh"

# Create a temporary directory for test files
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

PASS=0
FAIL=0

assert_executable() {
    local file="$1"
    local msg="$2"
    if [ -x "$file" ]; then
        echo "PASS: $msg"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $msg (expected executable, got non-executable)"
        FAIL=$((FAIL + 1))
    fi
}

assert_not_executable() {
    local file="$1"
    local msg="$2"
    if [ ! -x "$file" ]; then
        echo "PASS: $msg"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $msg (expected non-executable, got executable)"
        FAIL=$((FAIL + 1))
    fi
}

assert_content() {
    local file="$1"
    local expected="$2"
    local msg="$3"
    local actual
    actual=$(cat "$file")
    if [ "$actual" = "$expected" ]; then
        echo "PASS: $msg"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $msg (expected '$expected', got '$actual')"
        FAIL=$((FAIL + 1))
    fi
}

assert_file_missing() {
    local file="$1"
    local msg="$2"
    if [ ! -f "$file" ]; then
        echo "PASS: $msg"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $msg (file exists but should be missing)"
        FAIL=$((FAIL + 1))
    fi
}

assert_stderr_contains() {
    local stderr_text="$1"
    local pattern="$2"
    local msg="$3"
    if echo "$stderr_text" | grep -q "$pattern"; then
        echo "PASS: $msg"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $msg (stderr did not contain '$pattern')"
        FAIL=$((FAIL + 1))
    fi
}

# --- Test 1: Overwriting an executable file preserves the executable bit ---
echo "--- Test 1: Overwriting executable file preserves +x ---"
echo "#!/bin/bash" > "$TEST_DIR/dst_exec.sh"
chmod +x "$TEST_DIR/dst_exec.sh"

echo "new content" > "$TEST_DIR/src_noexec.txt"
chmod -x "$TEST_DIR/src_noexec.txt"

"$SCRIPT" "$TEST_DIR/src_noexec.txt" "$TEST_DIR/dst_exec.sh"

assert_executable "$TEST_DIR/dst_exec.sh" "Executable bit preserved after overwrite"
assert_content "$TEST_DIR/dst_exec.sh" "new content" "Content updated correctly"

# --- Test 2: Non-executable destination stays non-executable ---
echo "--- Test 2: Non-executable destination stays non-executable ---"
echo "old content" > "$TEST_DIR/dst_noexec.txt"
chmod -x "$TEST_DIR/dst_noexec.txt"

echo "new content 2" > "$TEST_DIR/src2.txt"
chmod -x "$TEST_DIR/src2.txt"

"$SCRIPT" "$TEST_DIR/src2.txt" "$TEST_DIR/dst_noexec.txt"

assert_not_executable "$TEST_DIR/dst_noexec.txt" "Non-executable stays non-executable"
assert_content "$TEST_DIR/dst_noexec.txt" "new content 2" "Content updated correctly"

# --- Test 3: New file (no existing destination) gets source permissions ---
echo "--- Test 3: New file gets source permissions ---"
echo "brand new" > "$TEST_DIR/src3.txt"
chmod -x "$TEST_DIR/src3.txt"

"$SCRIPT" "$TEST_DIR/src3.txt" "$TEST_DIR/new_dst.txt"

assert_not_executable "$TEST_DIR/new_dst.txt" "New non-executable file created without +x"
assert_content "$TEST_DIR/new_dst.txt" "brand new" "Content written correctly"

# --- Test 4: New file from executable source preserves source +x ---
echo "--- Test 4: Executable source creates executable destination ---"
echo "#!/bin/bash" > "$TEST_DIR/src_exec.sh"
chmod +x "$TEST_DIR/src_exec.sh"

"$SCRIPT" "$TEST_DIR/src_exec.sh" "$TEST_DIR/new_exec_dst.sh"

assert_executable "$TEST_DIR/new_exec_dst.sh" "Executable source creates executable destination"

# --- Test 5: Subdirectory creation works ---
echo "--- Test 5: Subdirectory creation ---"
echo "nested" > "$TEST_DIR/src5.txt"

"$SCRIPT" "$TEST_DIR/src5.txt" "$TEST_DIR/sub/dir/dst5.txt"

assert_content "$TEST_DIR/sub/dir/dst5.txt" "nested" "File created in nested directory"

# --- Test 6: Worktree cwd rejects main-repo destination ---
echo "--- Test 6: Worktree cwd rejects main-repo destination ---"
mkdir -p "$TEST_DIR/fake-repo/.claude/worktrees/agent-test"
echo "test content" > "$TEST_DIR/src6.txt"
set +e
STDERR6=$(WRITE_CLAUDE_CWD="$TEST_DIR/fake-repo/.claude/worktrees/agent-test" \
    "$SCRIPT" "$TEST_DIR/src6.txt" "$TEST_DIR/fake-repo/.claude/skills/foo/SKILL.md" 2>&1 >/dev/null)
RC6=$?
set -e
# Assert non-zero exit
if [ "$RC6" -ne 0 ]; then
    echo "PASS: Non-zero exit on main-repo destination from worktree"
    PASS=$((PASS + 1))
else
    echo "FAIL: Expected non-zero exit, got 0"
    FAIL=$((FAIL + 1))
fi
assert_file_missing "$TEST_DIR/fake-repo/.claude/skills/foo/SKILL.md" "File not created at main-repo destination"
assert_stderr_contains "$STDERR6" "BLOCKED" "Stderr contains BLOCKED"
assert_stderr_contains "$STDERR6" "agent-test" "Stderr mentions worktree path"

# --- Test 7: Worktree cwd accepts worktree-prefixed destination ---
echo "--- Test 7: Worktree cwd accepts worktree-prefixed destination ---"
echo "worktree content" > "$TEST_DIR/src7.txt"
WRITE_CLAUDE_CWD="$TEST_DIR/fake-repo/.claude/worktrees/agent-test" \
    "$SCRIPT" "$TEST_DIR/src7.txt" \
    "$TEST_DIR/fake-repo/.claude/worktrees/agent-test/.claude/skills/foo/SKILL.md"
assert_content "$TEST_DIR/fake-repo/.claude/worktrees/agent-test/.claude/skills/foo/SKILL.md" \
    "worktree content" "File created at correct worktree destination"

# --- Test 8: Non-worktree cwd allows any destination ---
echo "--- Test 8: Non-worktree cwd allows any destination ---"
echo "plain content" > "$TEST_DIR/src8.txt"
"$SCRIPT" "$TEST_DIR/src8.txt" "$TEST_DIR/dst8.txt"
assert_content "$TEST_DIR/dst8.txt" "plain content" "Non-worktree cwd allows write to any path"

# --- Summary ---
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
