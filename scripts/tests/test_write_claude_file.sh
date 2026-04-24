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

# --- Test 6: Default-mode success is silent (no output) ---
echo "--- Test 6: Default-mode success is silent ---"
echo "content" > "$TEST_DIR/src6.txt"
all_output=$("$SCRIPT" "$TEST_DIR/src6.txt" "$TEST_DIR/dst6.txt" 2>&1) || true
line_count=$(echo "$all_output" | grep -c . || true)
if [ "$line_count" -eq 0 ]; then
    echo "PASS: default-mode success is silent (0 lines)"
    PASS=$((PASS + 1))
else
    echo "FAIL: default-mode success is silent (got $line_count lines: $all_output)"
    FAIL=$((FAIL + 1))
fi

# --- Test 7: --verbose flag restores Copied line ---
echo "--- Test 7: --verbose restores Copied line ---"
echo "content7" > "$TEST_DIR/src7.txt"
verbose_output=$("$SCRIPT" --verbose "$TEST_DIR/src7.txt" "$TEST_DIR/dst7.txt" 2>&1) || true
if echo "$verbose_output" | grep -qi "copied\|src7\|dst7"; then
    echo "PASS: --verbose shows Copied line"
    PASS=$((PASS + 1))
else
    echo "FAIL: --verbose shows Copied line (got: $verbose_output)"
    FAIL=$((FAIL + 1))
fi

# --- Test 8: JM_VERBOSE=1 restores Copied line ---
echo "--- Test 8: JM_VERBOSE=1 restores Copied line ---"
echo "content8" > "$TEST_DIR/src8.txt"
env_verbose_output=$(JM_VERBOSE=1 "$SCRIPT" "$TEST_DIR/src8.txt" "$TEST_DIR/dst8.txt" 2>&1) || true
if echo "$env_verbose_output" | grep -qi "copied\|src8\|dst8"; then
    echo "PASS: JM_VERBOSE=1 shows Copied line"
    PASS=$((PASS + 1))
else
    echo "FAIL: JM_VERBOSE=1 shows Copied line (got: $env_verbose_output)"
    FAIL=$((FAIL + 1))
fi

# --- Test 9: Failure path stays loud ---
echo "--- Test 9: Failure path stays loud ---"
exit_code=0
stderr_fail=$("$SCRIPT" "$TEST_DIR/nonexistent.txt" "$TEST_DIR/dst9.txt" 2>&1 >/dev/null) || exit_code=$?
if [ "$exit_code" -ne 0 ]; then
    echo "PASS: exits non-zero on missing source"
    PASS=$((PASS + 1))
else
    echo "FAIL: exits non-zero on missing source (expected non-zero, got 0)"
    FAIL=$((FAIL + 1))
fi
if echo "$stderr_fail" | grep -qi "error\|does not exist"; then
    echo "PASS: failure path contains error message"
    PASS=$((PASS + 1))
else
    echo "FAIL: failure path contains error message (got: $stderr_fail)"
    FAIL=$((FAIL + 1))
fi

# --- Summary ---
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
