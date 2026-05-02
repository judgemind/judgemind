#!/usr/bin/env bash
# test_ecs_run.sh — Tests for ecs-run.sh exec-agent readiness probe (#3896).
#
# Validates that ecs-run.sh sources _ecs_exec_lib.sh and calls
# wait_for_exec_agent_ready before executing the user command.
#
# The mock aws returns a fake task ARN for list-tasks and simulates three
# probe scenarios for execute-command:
#   1. Probe succeeds immediately — no retry messages, command runs.
#   2. Probe is retryable N times then succeeds — retry messages printed.
#   3. Probe always retryable — deadline exceeded, script exits non-zero.
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ECS_RUN="$SCRIPT_DIR/ecs-run.sh"
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

# setup_mock_aws_exec_agent <retryable_calls>
#
# Creates a mock aws CLI that:
#   - Returns a fake task ARN for `aws ecs list-tasks`
#   - For `aws ecs execute-command`:
#       * returns the retryable InvalidParameterException error for the first
#         <retryable_calls> calls (or always if retryable_calls == -1)
#       * exits 0 on subsequent calls
#
# Prints the mock_bin path.
setup_mock_aws_exec_agent() {
    local retryable_calls="$1"
    local tmpdir
    tmpdir=$(make_temp_dir)
    local mock_bin="$tmpdir/bin"
    local state_file="$tmpdir/exec_call_count"
    mkdir -p "$mock_bin"
    echo "0" > "$state_file"

    local err_file="$tmpdir/exec_err.txt"
    printf 'An error occurred (InvalidParameterException) when calling the ExecuteCommand operation: The execute command agent is not running. Please wait until the exec agent on your task is ready.\n' \
        > "$err_file"

    cat > "$mock_bin/aws" << MOCK_AWS
#!/usr/bin/env bash
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "list-tasks" ]]; then
    echo "arn:aws:ecs:us-west-2:000000000000:task/fake-cluster/fake-ecs-run-task"
    exit 0
fi
if [[ "\${1:-}" == "ecs" && "\${2:-}" == "execute-command" ]]; then
    count=\$(cat "$state_file")
    count=\$((count + 1))
    echo "\$count" > "$state_file"
    if [[ $retryable_calls -eq -1 || \$count -le $retryable_calls ]]; then
        cat "$err_file" >&2
        exit 1
    fi
    # Simulate a successful execute-command (user command reached).
    echo "Session Manager plugin ran successfully"
    exit 0
fi
echo "Mock aws: unexpected command: \$*" >&2
exit 1
MOCK_AWS
    chmod +x "$mock_bin/aws"
    echo "$mock_bin"
}

run_script() {
    # Args: <mock_bin> <extra env vars as VAR=val...> -- <ecs-run.sh args>
    local mock_bin="$1"
    shift
    PATH="$mock_bin:$PATH" "$ECS_RUN" "$@" 2>&1
}

# ── Tests ──────────────────────────────────────────────────────────────────

test_exec_agent_probe_success() {
    # Probe exits 0 on first execute-command call. No retry messages should
    # appear, and the script should proceed to run the user command (not exit
    # from the probe-failure path).
    local mock_bin
    mock_bin=$(setup_mock_aws_exec_agent 0)
    local output rc=0
    output=$(EXEC_AGENT_POLL_TIMEOUT_SECS=10 \
        run_script "$mock_bin" "true" 2>&1) || rc=$?

    if [[ "$output" == *"exec agent"*"not ready"* ]]; then
        fail "exec_agent_probe_success: no retry message expected" "got: $output"
        return
    fi
    if [[ "$output" == *"ECS exec agent on task"*"did not come up"* ]]; then
        fail "exec_agent_probe_success: must not emit deadline message" "got: $output"
        return
    fi
    # The user command was reached (mock prints success string).
    if [[ "$output" != *"Session Manager plugin ran successfully"* ]]; then
        fail "exec_agent_probe_success: user command should have been reached" "got: $output"
        return
    fi
    pass "exec_agent_probe_success: probe succeeds immediately, user command runs"
}

test_exec_agent_probe_retries() {
    # Probe fails with retryable error on calls 1–2, succeeds on call 3.
    # Script should emit retry messages and eventually reach the user command.
    local mock_bin
    mock_bin=$(setup_mock_aws_exec_agent 2)
    local output rc=0
    output=$(EXEC_AGENT_POLL_TIMEOUT_SECS=20 \
        run_script "$mock_bin" "true" 2>&1) || rc=$?

    if [[ "$output" != *"exec agent"*"not ready"* ]]; then
        fail "exec_agent_probe_retries: expected retry message in output" "got: $output"
        return
    fi
    if [[ "$output" == *"ECS exec agent on task"*"did not come up"* ]]; then
        fail "exec_agent_probe_retries: must not emit deadline message when probe eventually succeeds" \
            "got: $output"
        return
    fi
    # User command must have been reached after the probe succeeded.
    if [[ "$output" != *"Session Manager plugin ran successfully"* ]]; then
        fail "exec_agent_probe_retries: user command should have been reached after retries" \
            "got: $output"
        return
    fi
    pass "exec_agent_probe_retries: retry messages printed, user command runs after agent ready"
}

test_exec_agent_probe_deadline_msg() {
    # Probe always returns retryable error. Script must exit non-zero and print
    # the specific deadline message. The user command must NOT be reached.
    local mock_bin
    mock_bin=$(setup_mock_aws_exec_agent -1)
    local output rc=0
    output=$(EXEC_AGENT_POLL_TIMEOUT_SECS=2 \
        run_script "$mock_bin" "true" 2>&1) || rc=$?

    if [[ $rc -eq 0 ]]; then
        fail "exec_agent_probe_deadline_msg: must exit non-zero on deadline" "got: $output"
        return
    fi
    if [[ "$output" != *"ECS exec agent on task"*"did not come up"* ]]; then
        fail "exec_agent_probe_deadline_msg: must print specific deadline message" "got: $output"
        return
    fi
    if [[ "$output" == *"Session Manager plugin ran successfully"* ]]; then
        fail "exec_agent_probe_deadline_msg: user command must NOT be reached on probe failure" \
            "got: $output"
        return
    fi
    pass "exec_agent_probe_deadline_msg: exits non-zero with specific deadline message, user command not reached"
}

# ── Run all ────────────────────────────────────────────────────────────────

test_exec_agent_probe_success
test_exec_agent_probe_retries
test_exec_agent_probe_deadline_msg

echo ""
echo "────────────────────────────────────────────────"
echo "Ran $TESTS tests, $FAILURES failed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
