#!/usr/bin/env bash
# test_wait_for_rollout.sh — Unit tests for scripts/wait-for-rollout.sh
#
# Exercises the ECS rollout polling loop against a mock aws CLI that emits a
# sequence of pre-canned describe-services responses. Verifies the success,
# failure, and timeout paths — both for task-def-ARN selection (used by the
# CI composite action, see #2519) and for deployment-id selection (used by
# scripts/ecs-redeploy.sh for operator manual redeploys, see #2523).
#
# Usage:
#   scripts/tests/test_wait_for_rollout.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_UNDER_TEST="$REPO_ROOT/scripts/wait-for-rollout.sh"

if [[ ! -x "$SCRIPT_UNDER_TEST" ]]; then
    echo "FATAL: $SCRIPT_UNDER_TEST is not executable." >&2
    exit 1
fi

FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
# shellcheck disable=SC2329  # invoked via trap below
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
    # Pre-create the responses dir so callers can drop canned JSON files into
    # it before setup_mock_aws runs (setup_mock_aws also mkdirs it, but only
    # when run_script is invoked).
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
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# Create a mock `aws` CLI that reads a sequence of JSON blobs from a directory
# and emits each one on successive calls. Writes arg history to args.log.
#
# Usage: setup_mock_aws <tmpdir> — returns path to the mock script.
#
# The caller should drop JSON response files named 01.json, 02.json, ... into
# <tmpdir>/responses/; each describe-services call emits the next one in order,
# wrapping back to the last file once exhausted.
setup_mock_aws() {
    local tmpdir="$1"
    local mock="$tmpdir/aws"

    mkdir -p "$tmpdir/responses"

    cat > "$mock" << 'MOCK'
#!/usr/bin/env bash
# Mock aws CLI — emits the next canned response from $RESPONSES_DIR/NN.json.
set -euo pipefail

RESPONSES_DIR="${RESPONSES_DIR:?RESPONSES_DIR required}"
CALL_LOG="${CALL_LOG:-/dev/null}"
CALL_COUNTER_FILE="${CALL_COUNTER_FILE:?CALL_COUNTER_FILE required}"

echo "$*" >> "$CALL_LOG"

# Only describe-services calls consume responses; update-service is a no-op here
# (the script we're testing doesn't call it — only action.yml does).
case "$*" in
    "ecs describe-services"*)
        :
        ;;
    *)
        # Unknown mock call — just exit 0.
        exit 0
        ;;
esac

# Read and increment call counter
if [[ -f "$CALL_COUNTER_FILE" ]]; then
    COUNT=$(cat "$CALL_COUNTER_FILE")
else
    COUNT=0
fi
COUNT=$((COUNT + 1))
echo "$COUNT" > "$CALL_COUNTER_FILE"

# Pick response file; clamp to highest available.
RESPONSE_FILE=$(printf '%s/%02d.json' "$RESPONSES_DIR" "$COUNT")
if [[ ! -f "$RESPONSE_FILE" ]]; then
    # Fall back to the highest-numbered file
    RESPONSE_FILE=$(ls -1 "$RESPONSES_DIR"/*.json 2>/dev/null | sort | tail -n 1 || echo "")
fi

if [[ -z "$RESPONSE_FILE" || ! -f "$RESPONSE_FILE" ]]; then
    echo "MOCK ERROR: no response file available" >&2
    exit 1
fi

cat "$RESPONSE_FILE"
MOCK
    chmod +x "$mock"
    echo "$mock"
}

# Write a describe-services response with the given deployment state.
# Usage: write_response <file> <rollout_state> <running> <desired> <pending> [task_def_arn] [deployment_id]
write_response() {
    local file="$1"
    local rollout_state="$2"
    local running="$3"
    local desired="$4"
    local pending="$5"
    local task_def_arn="${6:-arn:test:new}"
    local deployment_id="${7:-ecs-svc/new}"

    cat > "$file" << JSON
{
  "services": [
    {
      "serviceName": "test-svc",
      "desiredCount": $desired,
      "runningCount": $running,
      "pendingCount": $pending,
      "deployments": [
        {
          "id": "$deployment_id",
          "taskDefinition": "$task_def_arn",
          "rolloutState": "$rollout_state",
          "rolloutStateReason": "test reason",
          "runningCount": $running,
          "desiredCount": $desired,
          "pendingCount": $pending
        }
      ],
      "events": []
    }
  ]
}
JSON
}

# Write a response representing a --force-new-deployment redeploy: two
# deployments sharing the same task-def ARN but with distinct deployment
# IDs (the old primary being replaced + the new primary we're tracking).
# Usage: write_response_redeploy <file> <rollout_state> <running> <desired> <pending>
write_response_redeploy() {
    local file="$1"
    local rollout_state="$2"
    local running="$3"
    local desired="$4"
    local pending="$5"

    cat > "$file" << JSON
{
  "services": [
    {
      "serviceName": "test-svc",
      "desiredCount": $desired,
      "runningCount": $running,
      "pendingCount": $pending,
      "deployments": [
        {
          "id": "ecs-svc/new-redeploy",
          "taskDefinition": "arn:test:shared",
          "rolloutState": "$rollout_state",
          "rolloutStateReason": "redeploy test reason",
          "runningCount": $running,
          "desiredCount": $desired,
          "pendingCount": $pending
        },
        {
          "id": "ecs-svc/old-primary",
          "taskDefinition": "arn:test:shared",
          "rolloutState": "COMPLETED",
          "rolloutStateReason": "old",
          "runningCount": 1,
          "desiredCount": 1,
          "pendingCount": 0
        }
      ],
      "events": []
    }
  ]
}
JSON
}

# Write a describe-services response where our task def is NOT yet in the
# deployments array (simulating the brief window after update-service).
write_response_missing_deployment() {
    local file="$1"
    cat > "$file" << 'JSON'
{
  "services": [
    {
      "serviceName": "test-svc",
      "desiredCount": 1,
      "runningCount": 1,
      "pendingCount": 0,
      "deployments": [
        {
          "id": "ecs-svc/old",
          "taskDefinition": "arn:test:old",
          "rolloutState": "COMPLETED",
          "runningCount": 1,
          "desiredCount": 1,
          "pendingCount": 0
        }
      ],
      "events": []
    }
  ]
}
JSON
}

# Run the script under test with mocks wired up (task-def-ARN selector).
run_script() {
    local tmpdir="$1"
    shift
    local mock_aws
    mock_aws=$(setup_mock_aws "$tmpdir")

    AWS_CLI="$mock_aws" \
    RESPONSES_DIR="$tmpdir/responses" \
    CALL_LOG="$tmpdir/calls.log" \
    CALL_COUNTER_FILE="$tmpdir/counter" \
    ECS_CLUSTER="test-cluster" \
    ECS_SERVICE="test-svc" \
    NEW_TASK_DEF_ARN="arn:test:new" \
    ROLLOUT_POLL_INTERVAL="${ROLLOUT_POLL_INTERVAL:-0}" \
    ROLLOUT_TIMEOUT_SECS="${ROLLOUT_TIMEOUT_SECS:-5}" \
        "$SCRIPT_UNDER_TEST" "$@"
}

# Run the script under test with mocks wired up (deployment-id selector).
run_script_by_deployment_id() {
    local tmpdir="$1"
    local deployment_id="${2:-ecs-svc/new-redeploy}"
    shift 2 || shift $#
    local mock_aws
    mock_aws=$(setup_mock_aws "$tmpdir")

    AWS_CLI="$mock_aws" \
    RESPONSES_DIR="$tmpdir/responses" \
    CALL_LOG="$tmpdir/calls.log" \
    CALL_COUNTER_FILE="$tmpdir/counter" \
    ECS_CLUSTER="test-cluster" \
    ECS_SERVICE="test-svc" \
    NEW_DEPLOYMENT_ID="$deployment_id" \
    ROLLOUT_POLL_INTERVAL="${ROLLOUT_POLL_INTERVAL:-0}" \
    ROLLOUT_TIMEOUT_SECS="${ROLLOUT_TIMEOUT_SECS:-5}" \
        "$SCRIPT_UNDER_TEST" "$@"
}

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: Immediate COMPLETED with running==desired exits 0.
test_immediate_completed() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/responses"
    write_response "$tmpdir/responses/01.json" "COMPLETED" 1 1 0

    local output exit_code
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?
    exit_code="${exit_code:-0}"

    if [[ "$exit_code" -eq 0 ]]; then
        pass "immediate COMPLETED exits 0"
    else
        fail "immediate COMPLETED exits 0" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "Service updated successfully"; then
        pass "immediate COMPLETED emits success message"
    else
        fail "immediate COMPLETED emits success message" "output=$output"
    fi
}

# Test 2: IN_PROGRESS then COMPLETED exits 0.
test_in_progress_then_completed() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/responses"
    write_response "$tmpdir/responses/01.json" "IN_PROGRESS" 0 1 1
    write_response "$tmpdir/responses/02.json" "IN_PROGRESS" 1 1 0
    write_response "$tmpdir/responses/03.json" "COMPLETED" 1 1 0

    local output exit_code
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?
    exit_code="${exit_code:-0}"

    if [[ "$exit_code" -eq 0 ]]; then
        pass "IN_PROGRESS then COMPLETED exits 0"
    else
        fail "IN_PROGRESS then COMPLETED exits 0" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "rolloutState=IN_PROGRESS"; then
        pass "progress output logged during IN_PROGRESS"
    else
        fail "progress output logged during IN_PROGRESS" "output=$output"
    fi
}

# Test 3: Deployment missing then appearing as COMPLETED exits 0.
test_deployment_missing_then_appears() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/responses"
    write_response_missing_deployment "$tmpdir/responses/01.json"
    write_response "$tmpdir/responses/02.json" "COMPLETED" 1 1 0

    local output exit_code
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?
    exit_code="${exit_code:-0}"

    if [[ "$exit_code" -eq 0 ]]; then
        pass "missing-then-present deployment exits 0"
    else
        fail "missing-then-present deployment exits 0" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "not yet visible"; then
        pass "emits 'not yet visible' when deployment missing"
    else
        fail "emits 'not yet visible' when deployment missing" "output=$output"
    fi
}

# Test 4: FAILED rolloutState exits 1 with clear error.
test_failed_rollout() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/responses"
    write_response "$tmpdir/responses/01.json" "FAILED" 0 1 0

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "FAILED rollout exits 1"
    else
        fail "FAILED rollout exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "rolloutState=FAILED"; then
        pass "FAILED rollout emits clear error"
    else
        fail "FAILED rollout emits clear error" "output=$output"
    fi
}

# Test 5: Timeout exits 1 with diagnostic output.
test_timeout() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    # Always returns IN_PROGRESS — will never reach COMPLETED.
    write_response "$tmpdir/responses/01.json" "IN_PROGRESS" 0 1 1

    local output exit_code
    exit_code=0
    # Tight timeout to keep the test fast.
    output=$(ROLLOUT_TIMEOUT_SECS=1 ROLLOUT_POLL_INTERVAL=0 run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "timeout exits 1"
    else
        fail "timeout exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "Timed out after"; then
        pass "timeout emits clear error"
    else
        fail "timeout emits clear error" "output=$output"
    fi

    if echo "$output" | grep -q "Full service JSON snapshot"; then
        pass "timeout includes diagnostic JSON snapshot"
    else
        fail "timeout includes diagnostic JSON snapshot" "output=$output"
    fi
}

# Test 6: COMPLETED with running!=desired keeps polling until real completion.
test_completed_but_running_not_yet_matching() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    # First call: rolloutState claims COMPLETED but runningCount is still 0 —
    # we should NOT treat this as success. (Defensive: shouldn't happen in
    # practice since ECS only sets COMPLETED after steady state, but we guard
    # against the race.) Second call: true completion.
    write_response "$tmpdir/responses/01.json" "COMPLETED" 0 1 0
    write_response "$tmpdir/responses/02.json" "COMPLETED" 1 1 0

    local output exit_code
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?
    exit_code="${exit_code:-0}"

    if [[ "$exit_code" -eq 0 ]]; then
        pass "COMPLETED with running=0 waits for running=desired"
    else
        fail "COMPLETED with running=0 waits for running=desired" "exit=$exit_code output=$output"
    fi
}

# Test 7: aws CLI failure exits 1 quickly.
test_aws_cli_failure() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    # Create a mock that always exits 1
    local mock="$tmpdir/aws"
    cat > "$mock" << 'MOCK'
#!/usr/bin/env bash
echo "mock aws error" >&2
exit 1
MOCK
    chmod +x "$mock"

    local output exit_code
    exit_code=0
    output=$(AWS_CLI="$mock" \
        ECS_CLUSTER="c" ECS_SERVICE="s" NEW_TASK_DEF_ARN="arn:x" \
        ROLLOUT_TIMEOUT_SECS=5 ROLLOUT_POLL_INTERVAL=0 \
        "$SCRIPT_UNDER_TEST" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "aws CLI failure exits 1"
    else
        fail "aws CLI failure exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "describe-services failed"; then
        pass "aws CLI failure emits clear error"
    else
        fail "aws CLI failure emits clear error" "output=$output"
    fi
}

# Test 8: Missing required env vars exits non-zero.
test_missing_required_env() {
    local output exit_code
    exit_code=0
    output=$(env -i PATH="$PATH" bash "$SCRIPT_UNDER_TEST" 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        pass "missing required env exits non-zero"
    else
        fail "missing required env exits non-zero" "exit=$exit_code output=$output"
    fi
}

# Test 9: Neither NEW_TASK_DEF_ARN nor NEW_DEPLOYMENT_ID set exits 1 with
# a clear error. Guards the env-var contract for the dual selector.
test_missing_selector_env() {
    local output exit_code
    exit_code=0
    output=$(env -i PATH="$PATH" \
        ECS_CLUSTER="c" ECS_SERVICE="s" \
        bash "$SCRIPT_UNDER_TEST" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "missing both selectors exits 1"
    else
        fail "missing both selectors exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "one of NEW_TASK_DEF_ARN or NEW_DEPLOYMENT_ID"; then
        pass "missing both selectors emits clear error"
    else
        fail "missing both selectors emits clear error" "output=$output"
    fi
}

# Test 10: Setting both NEW_TASK_DEF_ARN and NEW_DEPLOYMENT_ID exits 1.
# The caller must pick exactly one selector to avoid ambiguity.
test_both_selectors_set() {
    local output exit_code
    exit_code=0
    output=$(env -i PATH="$PATH" \
        ECS_CLUSTER="c" ECS_SERVICE="s" \
        NEW_TASK_DEF_ARN="arn:x" NEW_DEPLOYMENT_ID="ecs-svc/y" \
        bash "$SCRIPT_UNDER_TEST" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "both selectors set exits 1"
    else
        fail "both selectors set exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "set only one of"; then
        pass "both selectors set emits clear error"
    else
        fail "both selectors set emits clear error" "output=$output"
    fi
}

# Test 11: Deployment-id selector succeeds on immediate COMPLETED.
# Mirrors the operator `--force-new-deployment` path where task-def ARN
# is ambiguous but deployment ID is unique.
test_deployment_id_immediate_completed() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/responses"
    write_response_redeploy "$tmpdir/responses/01.json" "COMPLETED" 1 1 0

    local output exit_code
    output=$(run_script_by_deployment_id "$tmpdir" "ecs-svc/new-redeploy" 2>&1) || exit_code=$?
    exit_code="${exit_code:-0}"

    if [[ "$exit_code" -eq 0 ]]; then
        pass "deployment-id immediate COMPLETED exits 0"
    else
        fail "deployment-id immediate COMPLETED exits 0" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "Waiting for deployment ecs-svc/new-redeploy"; then
        pass "deployment-id selector names the deployment in log output"
    else
        fail "deployment-id selector names the deployment in log output" "output=$output"
    fi
}

# Test 12: Deployment-id selector picks OUR deployment even when another
# deployment shares the same task-def ARN. This is the motivating case
# for the deployment-id selector — force-new-deployment redeploys leave
# multiple deployments with the same task-def ARN briefly.
test_deployment_id_disambiguates_shared_taskdef() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/responses"
    # Both deployments share arn:test:shared. Our new one is IN_PROGRESS,
    # the old one is already COMPLETED. Selecting by task-def ARN would
    # match BOTH ambiguously (jq would emit two objects). Selecting by
    # deployment ID picks the IN_PROGRESS one — we should NOT exit 0.
    # Second response: our new deployment completes.
    write_response_redeploy "$tmpdir/responses/01.json" "IN_PROGRESS" 0 1 1
    write_response_redeploy "$tmpdir/responses/02.json" "COMPLETED" 1 1 0

    local output exit_code
    output=$(run_script_by_deployment_id "$tmpdir" "ecs-svc/new-redeploy" 2>&1) || exit_code=$?
    exit_code="${exit_code:-0}"

    if [[ "$exit_code" -eq 0 ]]; then
        pass "deployment-id disambiguates across shared-taskdef deployments"
    else
        fail "deployment-id disambiguates across shared-taskdef deployments" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "rolloutState=IN_PROGRESS"; then
        pass "deployment-id selector reported IN_PROGRESS before COMPLETED"
    else
        fail "deployment-id selector reported IN_PROGRESS before COMPLETED" "output=$output"
    fi
}

# Test 13: Deployment-id selector reports FAILED and exits 1.
test_deployment_id_failed_rollout() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/responses"
    write_response_redeploy "$tmpdir/responses/01.json" "FAILED" 0 1 0

    local output exit_code
    exit_code=0
    output=$(run_script_by_deployment_id "$tmpdir" "ecs-svc/new-redeploy" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "deployment-id FAILED rollout exits 1"
    else
        fail "deployment-id FAILED rollout exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "rolloutState=FAILED for deployment ecs-svc/new-redeploy"; then
        pass "deployment-id FAILED emits clear error naming the deployment"
    else
        fail "deployment-id FAILED emits clear error naming the deployment" "output=$output"
    fi
}

# Test 14: Deployment-id selector times out when rollout never completes.
test_deployment_id_timeout() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/responses"
    write_response_redeploy "$tmpdir/responses/01.json" "IN_PROGRESS" 0 1 1

    local output exit_code
    exit_code=0
    output=$(ROLLOUT_TIMEOUT_SECS=1 ROLLOUT_POLL_INTERVAL=0 \
        run_script_by_deployment_id "$tmpdir" "ecs-svc/new-redeploy" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "deployment-id timeout exits 1"
    else
        fail "deployment-id timeout exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "Timed out after"; then
        pass "deployment-id timeout emits clear error"
    else
        fail "deployment-id timeout emits clear error" "output=$output"
    fi

    if echo "$output" | grep -q "Full service JSON snapshot"; then
        pass "deployment-id timeout includes diagnostic JSON snapshot"
    else
        fail "deployment-id timeout includes diagnostic JSON snapshot" "output=$output"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_immediate_completed
test_in_progress_then_completed
test_deployment_missing_then_appears
test_failed_rollout
test_timeout
test_completed_but_running_not_yet_matching
test_aws_cli_failure
test_missing_required_env
test_missing_selector_env
test_both_selectors_set
test_deployment_id_immediate_completed
test_deployment_id_disambiguates_shared_taskdef
test_deployment_id_failed_rollout
test_deployment_id_timeout

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
