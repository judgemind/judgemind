#!/usr/bin/env bash
# test_ecs_logs.sh — Tests for ecs-logs.sh stream selection and fallback
#
# Creates mock aws commands to verify that ecs-logs.sh correctly:
#   - Selects the first accessible stream
#   - Falls back when the top stream is stale/inaccessible
#   - Outputs exactly one stream name in the "Stream:" line
#   - Reports an error when all streams are stale
#
# Usage:
#   scripts/tests/test_ecs_logs.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ECS_LOGS="$SCRIPT_DIR/ecs-logs.sh"
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

# Create a mock aws CLI that simulates describe-log-streams and get-log-events.
# The mock behavior is controlled by environment variables:
#   MOCK_STREAMS          — tab-separated stream names to return from describe-log-streams
#   MOCK_STALE_STREAMS    — comma-separated stream names that should fail get-log-events
#   MOCK_LOG_EVENTS_JSON  — JSON to return from get-log-events for accessible streams
setup_mock_aws() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    local mock_bin="$tmpdir/bin"
    mkdir -p "$mock_bin"

    # Write the mock aws script
    cat > "$mock_bin/aws" << 'MOCK_AWS'
#!/usr/bin/env bash
# Mock aws CLI for ecs-logs.sh tests

# Parse subcommand
if [[ "${1:-}" == "logs" ]]; then
    shift
    case "${1:-}" in
        describe-log-streams)
            # Return the configured streams
            if [[ -n "${MOCK_STREAMS:-}" ]]; then
                echo "$MOCK_STREAMS"
            else
                echo "None"
            fi
            exit 0
            ;;
        get-log-events)
            # Check which stream is being accessed
            local stream_name=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --log-stream-name) stream_name="$2"; shift 2 ;;
                    *) shift ;;
                esac
            done

            # Check if this stream is in the stale list
            if [[ -n "${MOCK_STALE_STREAMS:-}" ]]; then
                IFS=',' read -ra stale_list <<< "$MOCK_STALE_STREAMS"
                for stale in "${stale_list[@]}"; do
                    if [[ "$stream_name" == "$stale" ]]; then
                        echo "Error: ResourceNotFoundException" >&2
                        exit 1
                    fi
                done
            fi

            # Return mock events
            echo "${MOCK_LOG_EVENTS_JSON:-{\"events\":[{\"message\":\"mock log line\"}],\"nextForwardToken\":\"token123\"}}"
            exit 0
            ;;
        *)
            echo "Mock aws: unknown logs subcommand $1" >&2
            exit 1
            ;;
    esac
fi

echo "Mock aws: unknown service $1" >&2
exit 1
MOCK_AWS
    chmod +x "$mock_bin/aws"

    echo "$tmpdir"
}

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: No arguments prints usage error
test_no_args() {
    local output
    output=$("$ECS_LOGS" 2>&1 || true)
    if echo "$output" | grep -q "log group name is required"; then
        pass "no arguments prints usage error"
    else
        fail "no arguments prints usage error" "got: $output"
    fi
}

# Test 2: --help prints usage
test_help() {
    local output
    output=$("$ECS_LOGS" --help 2>&1 || true)
    if echo "$output" | grep -q "Usage:"; then
        pass "help flag prints usage"
    else
        fail "help flag prints usage" "got: $output"
    fi
}

# Test 3: Unknown option is rejected
test_unknown_option() {
    local output
    output=$("$ECS_LOGS" /ecs/test --unknown 2>&1 || true)
    if echo "$output" | grep -q "unknown option"; then
        pass "unknown option rejected"
    else
        fail "unknown option rejected" "got: $output"
    fi
}

# Test 4: First accessible stream is selected when top stream is healthy
test_healthy_stream_selected() {
    local tmpdir
    tmpdir=$(setup_mock_aws)

    local output
    output=$(
        PATH="$tmpdir/bin:$PATH" \
        MOCK_STREAMS=$'ecs/container/task-a\tecs/container/task-b\tecs/container/task-c' \
        MOCK_STALE_STREAMS="" \
        "$ECS_LOGS" /ecs/test-group --lines 1 2>&1
    ) || true

    if echo "$output" | grep -q "Stream:.*ecs/container/task-a"; then
        pass "healthy top stream is selected"
    else
        fail "healthy top stream is selected" "got: $output"
    fi
}

# Test 5: Falls back to second stream when first is stale
test_stale_stream_fallback() {
    local tmpdir
    tmpdir=$(setup_mock_aws)

    local output
    output=$(
        PATH="$tmpdir/bin:$PATH" \
        MOCK_STREAMS=$'ecs/container/stale-task\tecs/container/good-task\tecs/container/task-c' \
        MOCK_STALE_STREAMS="ecs/container/stale-task" \
        "$ECS_LOGS" /ecs/test-group --lines 1 2>&1
    ) || true

    # Should show warning about the stale stream
    if echo "$output" | grep -q "Warning:.*stale-task.*stale/inaccessible"; then
        pass "warns about stale stream"
    else
        fail "warns about stale stream" "got: $output"
    fi

    # Should select the second (good) stream
    if echo "$output" | grep -q "Stream:.*ecs/container/good-task"; then
        pass "falls back to second stream when first is stale"
    else
        fail "falls back to second stream when first is stale" "got: $output"
    fi
}

# Test 6: Falls back to third stream when first two are stale
test_multiple_stale_fallback() {
    local tmpdir
    tmpdir=$(setup_mock_aws)

    local output
    output=$(
        PATH="$tmpdir/bin:$PATH" \
        MOCK_STREAMS=$'ecs/container/stale-1\tecs/container/stale-2\tecs/container/good-task' \
        MOCK_STALE_STREAMS="ecs/container/stale-1,ecs/container/stale-2" \
        "$ECS_LOGS" /ecs/test-group --lines 1 2>&1
    ) || true

    # Should select the third stream
    if echo "$output" | grep -q "Stream:.*ecs/container/good-task"; then
        pass "falls back to third stream when first two are stale"
    else
        fail "falls back to third stream when first two are stale" "got: $output"
    fi
}

# Test 7: Error when all streams are stale
test_all_stale_error() {
    local tmpdir
    tmpdir=$(setup_mock_aws)

    local output
    local exit_code=0
    output=$(
        PATH="$tmpdir/bin:$PATH" \
        MOCK_STREAMS=$'ecs/container/stale-1\tecs/container/stale-2' \
        MOCK_STALE_STREAMS="ecs/container/stale-1,ecs/container/stale-2" \
        "$ECS_LOGS" /ecs/test-group --lines 1 2>&1
    ) || exit_code=$?

    if [[ $exit_code -ne 0 ]]; then
        pass "exits non-zero when all streams are stale"
    else
        fail "exits non-zero when all streams are stale" "exit code was 0"
    fi

    if echo "$output" | grep -q "all recent log streams.*stale or inaccessible"; then
        pass "error message mentions all streams are stale"
    else
        fail "error message mentions all streams are stale" "got: $output"
    fi
}

# Test 8: Stream output shows exactly one stream name (not multiple)
test_single_stream_in_output() {
    local tmpdir
    tmpdir=$(setup_mock_aws)

    local output
    output=$(
        PATH="$tmpdir/bin:$PATH" \
        MOCK_STREAMS=$'ecs/container/task-a\tecs/container/task-b' \
        MOCK_STALE_STREAMS="" \
        "$ECS_LOGS" /ecs/test-group --lines 1 2>&1
    ) || true

    # Count how many "Stream:" lines there are — should be exactly 1
    local stream_line_count
    stream_line_count=$(echo "$output" | grep -c "^Stream:" || true)
    if [[ "$stream_line_count" -eq 1 ]]; then
        pass "exactly one Stream: line in output"
    else
        fail "exactly one Stream: line in output" "found $stream_line_count Stream: lines in: $output"
    fi

    # The Stream: line should contain exactly one stream path (no tabs/spaces separating multiple)
    local stream_value
    stream_value=$(echo "$output" | grep "^Stream:" | sed 's/^Stream:[[:space:]]*//')
    if [[ "$stream_value" == "ecs/container/task-a" ]]; then
        pass "stream output contains exactly one stream name"
    else
        fail "stream output contains exactly one stream name" "got: '$stream_value'"
    fi
}

# Test 9: No streams at all returns error
test_no_streams() {
    local tmpdir
    tmpdir=$(setup_mock_aws)

    local output
    local exit_code=0
    output=$(
        PATH="$tmpdir/bin:$PATH" \
        MOCK_STREAMS="None" \
        "$ECS_LOGS" /ecs/test-group --lines 1 2>&1
    ) || exit_code=$?

    if [[ $exit_code -ne 0 ]]; then
        pass "exits non-zero when no streams exist"
    else
        fail "exits non-zero when no streams exist" "exit code was 0"
    fi

    if echo "$output" | grep -q "no log streams found"; then
        pass "error message for empty log group"
    else
        fail "error message for empty log group" "got: $output"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_no_args
test_help
test_unknown_option
test_healthy_stream_selected
test_stale_stream_fallback
test_multiple_stale_fallback
test_all_stale_error
test_single_stream_in_output
test_no_streams

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
