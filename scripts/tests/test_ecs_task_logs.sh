#!/usr/bin/env bash
# test_ecs_task_logs.sh — Tests for ecs-task-logs.sh argument handling
#
# Creates a mock ecs-logs.sh to verify that ecs-task-logs.sh correctly
# passes arguments through, including the case with no extra arguments
# (which previously failed with "unbound variable" under set -u).
#
# Usage:
#   scripts/tests/test_ecs_task_logs.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ECS_TASK_LOGS="$SCRIPT_DIR/ecs-task-logs.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
cleanup() {
    set +e
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
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

# Create a fake scripts directory with a mock ecs-logs.sh that records its
# arguments to a file instead of actually calling AWS.
setup_mock() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    local mock_scripts="$tmpdir/scripts"
    mkdir -p "$mock_scripts"

    # Copy the real ecs-task-logs.sh
    cp "$ECS_TASK_LOGS" "$mock_scripts/ecs-task-logs.sh"
    chmod +x "$mock_scripts/ecs-task-logs.sh"

    # Create a mock ecs-logs.sh that records args
    cat > "$mock_scripts/ecs-logs.sh" << 'MOCK'
#!/usr/bin/env bash
# Mock ecs-logs.sh — records arguments to MOCK_OUTPUT_FILE
echo "$@" > "${MOCK_OUTPUT_FILE:-/dev/null}"
exit 0
MOCK
    chmod +x "$mock_scripts/ecs-logs.sh"

    echo "$tmpdir"
}

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: No arguments prints usage error
test_no_args() {
    local output
    output=$("$ECS_TASK_LOGS" 2>&1 || true)
    if echo "$output" | grep -q "task ID or task ARN is required"; then
        pass "no arguments prints usage error"
    else
        fail "no arguments prints usage error" "got: $output"
    fi

    if "$ECS_TASK_LOGS" > /dev/null 2>&1; then
        fail "no arguments exits with code 1" "exit code was 0"
    else
        pass "no arguments exits with code 1"
    fi
}

# Test 2: Task ID without extra arguments (the bug this issue fixes)
test_task_id_no_extra_args() {
    local tmpdir
    tmpdir=$(setup_mock)

    local output_file="$tmpdir/args.txt"
    MOCK_OUTPUT_FILE="$output_file" "$tmpdir/scripts/ecs-task-logs.sh" abc123def456 2>/dev/null
    local exit_code=$?

    if [[ $exit_code -ne 0 ]]; then
        fail "task ID without extra args exits 0" "exit code was $exit_code"
        return
    fi
    pass "task ID without extra args exits 0"

    local recorded
    recorded=$(cat "$output_file")
    if echo "$recorded" | grep -q "/ecs/judgemind-ingestion-worker-dev"; then
        pass "task ID without extra args passes correct log group"
    else
        fail "task ID without extra args passes correct log group" "got: $recorded"
    fi

    if echo "$recorded" | grep -q -- "--task abc123def456"; then
        pass "task ID without extra args passes task ID"
    else
        fail "task ID without extra args passes task ID" "got: $recorded"
    fi
}

# Test 3: Task ID with --follow flag
test_task_id_with_follow() {
    local tmpdir
    tmpdir=$(setup_mock)

    local output_file="$tmpdir/args.txt"
    MOCK_OUTPUT_FILE="$output_file" "$tmpdir/scripts/ecs-task-logs.sh" abc123 --follow 2>/dev/null

    local recorded
    recorded=$(cat "$output_file")
    if echo "$recorded" | grep -q -- "--follow"; then
        pass "follow flag passed through to ecs-logs.sh"
    else
        fail "follow flag passed through to ecs-logs.sh" "got: $recorded"
    fi
}

# Test 4: Task ID with --lines option
test_task_id_with_lines() {
    local tmpdir
    tmpdir=$(setup_mock)

    local output_file="$tmpdir/args.txt"
    MOCK_OUTPUT_FILE="$output_file" "$tmpdir/scripts/ecs-task-logs.sh" abc123 --lines 100 2>/dev/null

    local recorded
    recorded=$(cat "$output_file")
    if echo "$recorded" | grep -q -- "--lines 100"; then
        pass "lines option passed through to ecs-logs.sh"
    else
        fail "lines option passed through to ecs-logs.sh" "got: $recorded"
    fi
}

# Test 5: Full task ARN extracts task ID
test_full_arn() {
    local tmpdir
    tmpdir=$(setup_mock)

    local output_file="$tmpdir/args.txt"
    MOCK_OUTPUT_FILE="$output_file" "$tmpdir/scripts/ecs-task-logs.sh" \
        "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123def456" 2>/dev/null

    local recorded
    recorded=$(cat "$output_file")
    if echo "$recorded" | grep -q -- "--task abc123def456"; then
        pass "full ARN correctly extracts task ID"
    else
        fail "full ARN correctly extracts task ID" "got: $recorded"
    fi
}

# Test 6: Custom environment
test_custom_env() {
    local tmpdir
    tmpdir=$(setup_mock)

    local output_file="$tmpdir/args.txt"
    MOCK_OUTPUT_FILE="$output_file" "$tmpdir/scripts/ecs-task-logs.sh" --env production abc123 2>/dev/null

    local recorded
    recorded=$(cat "$output_file")
    if echo "$recorded" | grep -q "/ecs/judgemind-ingestion-worker-production"; then
        pass "custom env uses correct log group"
    else
        fail "custom env uses correct log group" "got: $recorded"
    fi
}

# Test 7: --help flag
test_help() {
    local output
    output=$("$ECS_TASK_LOGS" --help 2>&1 || true)
    if echo "$output" | grep -q "Usage"; then
        pass "help flag prints usage"
    else
        fail "help flag prints usage" "got: $output"
    fi
}

# Test 8: Unknown option rejected
test_unknown_option() {
    local output
    output=$("$ECS_TASK_LOGS" --unknown abc123 2>&1 || true)
    if echo "$output" | grep -q "unknown option"; then
        pass "unknown option rejected"
    else
        fail "unknown option rejected" "got: $output"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_no_args
test_task_id_no_extra_args
test_task_id_with_follow
test_task_id_with_lines
test_full_arn
test_custom_env
test_help
test_unknown_option

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
