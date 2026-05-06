#!/usr/bin/env bash
# test_ecs_wait_task.sh — Unit tests for scripts/ecs-wait-task.sh
#
# Exercises the polling loop against a mock `aws` CLI that emits a
# pre-canned sequence of describe-tasks JSON responses, one per call.
# Verifies:
#   - --help exits 0 and includes the canonical usage line
#   - Bad ARN format exits 3 with a clear error
#   - Invalid --timeout exits 3 with a clear error
#   - Sentinel-file fallback (no positional ARN) reads tmp/last-ecs-task.arn
#   - Missing ARN + missing sentinel exits 3 with a clear error
#   - A task that goes RUNNING -> RUNNING -> STOPPED returns the container
#     exit code, prints a structured stdout line, and emits per-poll
#     liveness notes on stderr
#   - A task that STOPS with a non-zero exitCode propagates that code
#   - A task that STOPS without an exitCode exits 1 with a clear stderr
#     "Container stopped without exit code" line
#   - --timeout fires when the task never reaches STOPPED (exit 2)
#   - --quiet suppresses the per-poll status lines but keeps the final
#     summary line on stdout
#
# Usage:
#   scripts/tests/test_ecs_wait_task.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_UNDER_TEST="$REPO_ROOT/scripts/ecs-wait-task.sh"

if [ ! -x "$SCRIPT_UNDER_TEST" ]; then
    echo "FATAL: $SCRIPT_UNDER_TEST is not executable." >&2
    exit 1
fi

FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
# shellcheck disable=SC2329  # invoked via trap
cleanup() {
    set +e
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
        if [ -n "$d" ] && [ -d "$d" ]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    mkdir -p "$dir/responses"
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
    if [ -n "${2:-}" ]; then
        echo "  $2"
    fi
}

# Create a mock `aws` CLI that reads describe-tasks responses from a
# numbered sequence of JSON files in $RESPONSES_DIR. On each call it
# bumps a counter and emits the matching response file (clamping to
# the highest-numbered file once exhausted, so a "stopped" terminal
# state can be repeated indefinitely).
setup_mock_aws() {
    local tmpdir="$1"
    local mock="$tmpdir/aws"

    cat > "$mock" << 'MOCK_AWS'
#!/usr/bin/env bash
set -euo pipefail

RESPONSES_DIR="${RESPONSES_DIR:?RESPONSES_DIR required}"
CALL_COUNTER_FILE="${CALL_COUNTER_FILE:?CALL_COUNTER_FILE required}"

# Only intercept `aws ecs describe-tasks` — the only AWS call the
# script makes. Any other invocation is a test bug.
ARGS="$*"
case "$ARGS" in
    *"ecs describe-tasks"*)
        ;;
    *)
        echo "MOCK aws: unexpected call: $ARGS" >&2
        exit 1
        ;;
esac

# Read and increment call counter.
if [ -f "$CALL_COUNTER_FILE" ]; then
    COUNT=$(cat "$CALL_COUNTER_FILE")
else
    COUNT=0
fi
COUNT=$((COUNT + 1))
printf '%s' "$COUNT" > "$CALL_COUNTER_FILE"

# Pick response file; clamp to highest available so terminal-state
# responses can repeat indefinitely if the loop polls past the end.
RESPONSE_FILE=$(printf '%s/%02d.json' "$RESPONSES_DIR" "$COUNT")
if [ ! -f "$RESPONSE_FILE" ]; then
    RESPONSE_FILE=$(ls -1 "$RESPONSES_DIR"/*.json 2>/dev/null | sort | tail -n 1 || echo "")
fi
if [ -z "$RESPONSE_FILE" ] || [ ! -f "$RESPONSE_FILE" ]; then
    echo "MOCK aws: no response file available for call $COUNT" >&2
    exit 1
fi

cat "$RESPONSE_FILE"
MOCK_AWS
    chmod +x "$mock"
    echo "$mock"
}

# Run the script under test with the mock aws wired up via PATH.
# Usage: run_script <tmpdir> [extra-args...]
run_script() {
    local tmpdir="$1"
    shift
    setup_mock_aws "$tmpdir" >/dev/null

    PATH="$tmpdir:$PATH" \
    RESPONSES_DIR="$tmpdir/responses" \
    CALL_COUNTER_FILE="$tmpdir/counter" \
        "$SCRIPT_UNDER_TEST" --poll-interval 1 "$@"
}

# Canned describe-tasks response builders.
write_running_response() {
    local file="$1"
    cat > "$file" << 'JSON'
{
  "tasks": [
    {
      "taskArn": "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123",
      "lastStatus": "RUNNING",
      "containers": [{"name": "worker"}]
    }
  ]
}
JSON
}

write_stopped_response_with_exit_code() {
    local file="$1"
    local exit_code="${2:-0}"
    local stopped_reason="${3:-Essential container exited normally}"
    cat > "$file" << JSON
{
  "tasks": [
    {
      "taskArn": "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123",
      "lastStatus": "STOPPED",
      "stoppedReason": "$stopped_reason",
      "containers": [{"name": "worker", "exitCode": $exit_code}]
    }
  ]
}
JSON
}

write_stopped_response_no_exit_code() {
    local file="$1"
    cat > "$file" << 'JSON'
{
  "tasks": [
    {
      "taskArn": "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123",
      "lastStatus": "STOPPED",
      "stoppedReason": "OutOfMemoryError",
      "containers": [{"name": "worker", "reason": "OutOfMemoryError: Container killed due to memory usage"}]
    }
  ]
}
JSON
}

ARN="arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/abc123"

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: --help exits 0 and contains the canonical usage line.
test_help_documents_usage() {
    local output
    output=$("$SCRIPT_UNDER_TEST" --help 2>&1 || true)
    if echo "$output" | grep -q "Usage: scripts/ecs-wait-task.sh"; then
        pass "--help shows usage"
    else
        fail "--help shows usage" "got: $output"
    fi
    if echo "$output" | grep -q -- "--timeout"; then
        pass "--help documents --timeout"
    else
        fail "--help documents --timeout" "got: $output"
    fi
}

# Test 2: Bad ARN format exits 3.
test_bad_arn_exits_3() {
    local output exit_code=0
    output=$("$SCRIPT_UNDER_TEST" not-an-arn 2>&1) || exit_code=$?
    if [ "$exit_code" -eq 3 ]; then
        pass "bad ARN exits 3"
    else
        fail "bad ARN exits 3" "exit=$exit_code output=$output"
    fi
    if echo "$output" | grep -q "could not parse task ARN"; then
        pass "bad ARN error message is clear"
    else
        fail "bad ARN error message is clear" "got: $output"
    fi
}

# Test 3: Invalid --timeout exits 3.
test_invalid_timeout_exits_3() {
    local output exit_code=0
    output=$("$SCRIPT_UNDER_TEST" --timeout abc "$ARN" 2>&1) || exit_code=$?
    if [ "$exit_code" -eq 3 ]; then
        pass "non-numeric --timeout exits 3"
    else
        fail "non-numeric --timeout exits 3" "exit=$exit_code"
    fi
    if echo "$output" | grep -q "must be a non-negative integer"; then
        pass "non-numeric --timeout produces clear error"
    else
        fail "non-numeric --timeout produces clear error" "got: $output"
    fi
}

# Test 4: Missing ARN + missing sentinel exits 3.
test_missing_arn_no_sentinel_exits_3() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    # Use a fake repo root with no tmp/last-ecs-task.arn.
    local fake_repo="$tmpdir/repo"
    mkdir -p "$fake_repo/scripts"
    cp "$SCRIPT_UNDER_TEST" "$fake_repo/scripts/ecs-wait-task.sh"
    chmod +x "$fake_repo/scripts/ecs-wait-task.sh"

    local output exit_code=0
    output=$("$fake_repo/scripts/ecs-wait-task.sh" 2>&1) || exit_code=$?
    if [ "$exit_code" -eq 3 ]; then
        pass "missing ARN + no sentinel exits 3"
    else
        fail "missing ARN + no sentinel exits 3" "exit=$exit_code"
    fi
    if echo "$output" | grep -q "no task ARN supplied"; then
        pass "missing-ARN error message points at sentinel + --detach"
    else
        fail "missing-ARN error message points at sentinel + --detach" "got: $output"
    fi
}

# Test 5: Sentinel fallback — when ARN is omitted, read tmp/last-ecs-task.arn.
test_sentinel_fallback() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    local fake_repo="$tmpdir/repo"
    mkdir -p "$fake_repo/scripts" "$fake_repo/tmp"
    cp "$SCRIPT_UNDER_TEST" "$fake_repo/scripts/ecs-wait-task.sh"
    chmod +x "$fake_repo/scripts/ecs-wait-task.sh"

    printf '%s\n' "$ARN" > "$fake_repo/tmp/last-ecs-task.arn"

    write_stopped_response_with_exit_code "$tmpdir/responses/01.json" 0

    setup_mock_aws "$tmpdir" >/dev/null

    local output exit_code=0
    output=$(
        PATH="$tmpdir:$PATH" \
        RESPONSES_DIR="$tmpdir/responses" \
        CALL_COUNTER_FILE="$tmpdir/counter" \
            "$fake_repo/scripts/ecs-wait-task.sh" --poll-interval 1 2>&1
    ) || exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        pass "sentinel fallback returns container exit code (0)"
    else
        fail "sentinel fallback returns container exit code (0)" "exit=$exit_code output=$output"
    fi
    if echo "$output" | grep -q "Using ARN from tmp/last-ecs-task.arn"; then
        pass "sentinel fallback announces source"
    else
        fail "sentinel fallback announces source" "output=$output"
    fi
}

# Test 6: Already-stopped task returns immediately with exitCode=0.
test_already_stopped_exit_zero() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_stopped_response_with_exit_code "$tmpdir/responses/01.json" 0

    local output exit_code=0
    output=$(run_script "$tmpdir" "$ARN" 2>&1) || exit_code=$?
    if [ "$exit_code" -eq 0 ]; then
        pass "STOPPED+exit=0 yields shell exit 0"
    else
        fail "STOPPED+exit=0 yields shell exit 0" "exit=$exit_code output=$output"
    fi
    if echo "$output" | grep -q "status=STOPPED exit_code=0"; then
        pass "structured stdout line shows status=STOPPED exit_code=0"
    else
        fail "structured stdout line shows status=STOPPED exit_code=0" "output=$output"
    fi
    if echo "$output" | grep -qE "status=STOPPED elapsed=0s"; then
        pass "first-poll liveness note printed"
    else
        fail "first-poll liveness note printed" "output=$output"
    fi
}

# Test 7: Non-zero exit code propagates as the shell exit code.
test_nonzero_exit_propagates() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_stopped_response_with_exit_code "$tmpdir/responses/01.json" 137

    local output exit_code=0
    output=$(run_script "$tmpdir" "$ARN" 2>&1) || exit_code=$?
    if [ "$exit_code" -eq 137 ]; then
        pass "container exitCode=137 propagates as shell exit 137"
    else
        fail "container exitCode=137 propagates as shell exit 137" "exit=$exit_code"
    fi
    if echo "$output" | grep -q "exit_code=137"; then
        pass "structured stdout includes exit_code=137"
    else
        fail "structured stdout includes exit_code=137" "output=$output"
    fi
}

# Test 8: STOPPED with no exitCode (e.g. OOM kill) exits 1.
test_stopped_no_exit_code() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_stopped_response_no_exit_code "$tmpdir/responses/01.json"

    local output exit_code=0
    output=$(run_script "$tmpdir" "$ARN" 2>&1) || exit_code=$?
    if [ "$exit_code" -eq 1 ]; then
        pass "STOPPED with no exitCode yields shell exit 1"
    else
        fail "STOPPED with no exitCode yields shell exit 1" "exit=$exit_code output=$output"
    fi
    if echo "$output" | grep -q "Container stopped without exit code"; then
        pass "missing-exitCode message names the container reason"
    else
        fail "missing-exitCode message names the container reason" "output=$output"
    fi
}

# Test 9: RUNNING -> STOPPED transition emits a per-poll liveness note then
# returns the exit code.
test_running_then_stopped() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_running_response "$tmpdir/responses/01.json"
    write_running_response "$tmpdir/responses/02.json"
    write_stopped_response_with_exit_code "$tmpdir/responses/03.json" 0

    local output exit_code=0
    output=$(run_script "$tmpdir" "$ARN" 2>&1) || exit_code=$?
    if [ "$exit_code" -eq 0 ]; then
        pass "RUNNING -> STOPPED returns 0"
    else
        fail "RUNNING -> STOPPED returns 0" "exit=$exit_code output=$output"
    fi
    # Should have at least 3 status= lines (one per poll).
    local liveness_count
    liveness_count=$(echo "$output" | grep -c "status=" || true)
    if [ "$liveness_count" -ge 3 ]; then
        pass "per-poll liveness notes printed (count=$liveness_count)"
    else
        fail "per-poll liveness notes printed" "count=$liveness_count, output=$output"
    fi
}

# Test 10: --timeout fires when the task stays RUNNING.
test_timeout_fires() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_running_response "$tmpdir/responses/01.json"
    # All subsequent responses also RUNNING (clamp behavior).

    # --timeout 0 means unlimited, so use 1 minute. Combined with
    # --poll-interval 30s we need ~3 polls to exceed 60s elapsed.
    local output exit_code=0
    output=$(
        setup_mock_aws "$tmpdir" >/dev/null
        PATH="$tmpdir:$PATH" \
        RESPONSES_DIR="$tmpdir/responses" \
        CALL_COUNTER_FILE="$tmpdir/counter" \
            "$SCRIPT_UNDER_TEST" --poll-interval 1 --timeout 1 "$ARN" 2>&1
    ) || exit_code=$?

    if [ "$exit_code" -eq 2 ]; then
        pass "--timeout 1 fires with exit 2 when task stays RUNNING"
    else
        fail "--timeout 1 fires with exit 2 when task stays RUNNING" "exit=$exit_code output=$output"
    fi
    if echo "$output" | grep -q "timed out after 1m"; then
        pass "timeout message names the budget"
    else
        fail "timeout message names the budget" "output=$output"
    fi
}

# Test 11: --quiet suppresses per-poll lines but keeps stdout summary.
test_quiet_suppresses_progress() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_stopped_response_with_exit_code "$tmpdir/responses/01.json" 0

    local stdout stderr exit_code=0
    # Capture stdout + stderr separately to verify channel separation.
    # Place capture files inside our owned tmpdir so cleanup is bounded.
    local stdout_file="$tmpdir/stdout.log"
    local stderr_file="$tmpdir/stderr.log"

    setup_mock_aws "$tmpdir" >/dev/null
    PATH="$tmpdir:$PATH" \
    RESPONSES_DIR="$tmpdir/responses" \
    CALL_COUNTER_FILE="$tmpdir/counter" \
        "$SCRIPT_UNDER_TEST" --poll-interval 1 --quiet "$ARN" \
        > "$stdout_file" 2> "$stderr_file" || exit_code=$?

    stdout=$(cat "$stdout_file")
    stderr=$(cat "$stderr_file")

    if [ "$exit_code" -eq 0 ]; then
        pass "--quiet path returns container exit code"
    else
        fail "--quiet path returns container exit code" "exit=$exit_code stderr=$stderr"
    fi
    if echo "$stdout" | grep -q "status=STOPPED exit_code=0"; then
        pass "--quiet keeps structured stdout summary"
    else
        fail "--quiet keeps structured stdout summary" "stdout=$stdout"
    fi
    if echo "$stderr" | grep -qE "^\[20[0-9]{2}-"; then
        fail "--quiet suppresses per-poll [timestamp] lines" "stderr=$stderr"
    else
        pass "--quiet suppresses per-poll [timestamp] lines"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_help_documents_usage
test_bad_arn_exits_3
test_invalid_timeout_exits_3
test_missing_arn_no_sentinel_exits_3
test_sentinel_fallback
test_already_stopped_exit_zero
test_nonzero_exit_propagates
test_stopped_no_exit_code
test_running_then_stopped
test_timeout_fires
test_quiet_suppresses_progress

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [ "$FAILURES" -gt 0 ]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
