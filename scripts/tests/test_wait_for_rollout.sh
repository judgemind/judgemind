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
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

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
    "ecs describe-task-definition"*)
        # Superseded-deployment image check (#5073): emit
        # $TASKDEFS_DIR/<family:rev>.json for --task-definition <arn>.
        td=""
        prev=""
        for a in "$@"; do
            if [[ "$prev" == "--task-definition" ]]; then td="$a"; fi
            prev="$a"
        done
        f="${TASKDEFS_DIR:-/nonexistent}/${td##*/}.json"
        if [[ -f "$f" ]]; then
            cat "$f"
            exit 0
        fi
        echo "MOCK: no task definition for $td" >&2
        exit 254
        ;;
    "ecr describe-images"*)
        # Emit the digest stored in $ECR_DIR/<tag>; a missing file is a
        # deleted tag. $ECR_FAIL=1 simulates an outright API failure.
        if [[ "${ECR_FAIL:-0}" == "1" ]]; then
            echo "MOCK: ExpiredTokenException" >&2
            exit 255
        fi
        ids=""
        prev=""
        for a in "$@"; do
            if [[ "$prev" == "--image-ids" ]]; then ids="$a"; fi
            prev="$a"
        done
        tag="${ids#imageTag=}"
        f="${ECR_DIR:-/nonexistent}/$tag"
        if [[ -f "$f" ]]; then
            printf '{"imageDetails":[{"imageDigest":"%s"}]}\n' "$(cat "$f")"
            exit 0
        fi
        echo "An error occurred (ImageNotFoundException) when calling the DescribeImages operation" >&2
        exit 254
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
    TASKDEFS_DIR="$tmpdir/taskdefs" \
    ECR_DIR="$tmpdir/ecr" \
    CALL_LOG="$tmpdir/calls.log" \
    CALL_COUNTER_FILE="$tmpdir/counter" \
    ECS_CLUSTER="test-cluster" \
    ECS_SERVICE="test-svc" \
    NEW_TASK_DEF_ARN="${RUN_TASK_DEF_ARN:-arn:test:new}" \
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
    TASKDEFS_DIR="$tmpdir/taskdefs" \
    ECR_DIR="$tmpdir/ecr" \
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

# Test 5: Timeout exits 124 (still rolling out, #4835) with diagnostic output.
test_timeout() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    # Always returns IN_PROGRESS — will never reach COMPLETED.
    write_response "$tmpdir/responses/01.json" "IN_PROGRESS" 0 1 1

    local output exit_code
    exit_code=0
    # Tight timeout to keep the test fast.
    output=$(ROLLOUT_TIMEOUT_SECS=1 ROLLOUT_POLL_INTERVAL=0 run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 124 ]]; then
        pass "timeout exits 124 (still rolling out)"
    else
        fail "timeout exits 124 (still rolling out)" "exit=$exit_code output=$output"
    fi
    if echo "$output" | grep -q "Re-run with the same env vars"; then
        pass "timeout says to re-run to keep waiting"
    else
        fail "timeout says to re-run to keep waiting" "output=$output"
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

# Test 6a: COMPLETED with desiredCount=0 and runningCount=0 exits 0.
# Mirrors the scale-to-zero deployment case from #2585 — the dev ingestion
# worker is kept at desiredCount=0, and ECS marks new deployments COMPLETED
# immediately. The script used to require runningCount>0 and timed out in
# this case; now it treats running==desired==0 as a valid success state.
test_scale_to_zero_completed() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/responses"
    write_response "$tmpdir/responses/01.json" "COMPLETED" 0 0 0

    local output exit_code
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?
    exit_code="${exit_code:-0}"

    if [[ "$exit_code" -eq 0 ]]; then
        pass "scale-to-zero COMPLETED (desired=0, running=0) exits 0"
    else
        fail "scale-to-zero COMPLETED (desired=0, running=0) exits 0" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -qi "success"; then
        pass "scale-to-zero COMPLETED emits a success message"
    else
        fail "scale-to-zero COMPLETED emits a success message" "output=$output"
    fi
}

# Test 6b: deployment-id selector also accepts scale-to-zero COMPLETED.
# Covers the operator --force-new-deployment redeploy path (scripts/ecs-redeploy.sh)
# when the service is scaled to desiredCount=0.
test_deployment_id_scale_to_zero_completed() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    mkdir -p "$tmpdir/responses"
    write_response_redeploy "$tmpdir/responses/01.json" "COMPLETED" 0 0 0

    local output exit_code
    output=$(run_script_by_deployment_id "$tmpdir" "ecs-svc/new-redeploy" 2>&1) || exit_code=$?
    exit_code="${exit_code:-0}"

    if [[ "$exit_code" -eq 0 ]]; then
        pass "deployment-id scale-to-zero COMPLETED exits 0"
    else
        fail "deployment-id scale-to-zero COMPLETED exits 0" "exit=$exit_code output=$output"
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

    if [[ "$exit_code" -eq 125 ]]; then
        pass "aws CLI failure exits 125 (state unknown)"
    else
        fail "aws CLI failure exits 125 (state unknown)" "exit=$exit_code output=$output"
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

    if [[ "$exit_code" -eq 124 ]]; then
        pass "deployment-id timeout exits 124"
    else
        fail "deployment-id timeout exits 124" "exit=$exit_code output=$output"
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

# ── Superseded deployments (#5073) ─────────────────────────────────────────
#
# Models the PR #5070 incident: deploy-scraper registers :808 with
# image scraper:<sha7>, then terraform dev-apply force-deploys :809 with
# image scraper:latest. Both tags point at the same pushed image when the
# deploy-scraper build pushed them together.

TD_FAMILY_ARN="arn:aws:ecs:us-west-2:123456789012:task-definition/judgemind-ingestion-worker-dev"
OUR_TD="${TD_FAMILY_ARN}:808"
TF_TD="${TD_FAMILY_ARN}:809"
ECR_REPO_URI="123456789012.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper"
OUR_DIGEST="sha256:1111111111111111111111111111111111111111111111111111111111111111"
OLD_DIGEST="sha256:0000000000000000000000000000000000000000000000000000000000000000"

# write_services <file> <deployments-json-array>
write_services() {
    cat > "$1" << JSON
{"services": [{"serviceName": "test-svc", "desiredCount": 1, "runningCount": 1,
  "pendingCount": 0, "deployments": $2, "events": []}]}
JSON
}

# dep_json <id> <task-def> <status> <rollout-state> <running> <desired>
dep_json() {
    printf '{"id":"%s","taskDefinition":"%s","status":"%s","rolloutState":"%s","rolloutStateReason":"","runningCount":%s,"desiredCount":%s,"pendingCount":0}' \
        "$1" "$2" "$3" "$4" "$5" "$6"
}

# write_taskdef <tmpdir> <task-def-arn> <image>
write_taskdef() {
    mkdir -p "$1/taskdefs"
    printf '{"taskDefinition":{"taskDefinitionArn":"%s","containerDefinitions":[{"name":"ingestion-worker","image":"%s"}]}}\n' \
        "$2" "$3" > "$1/taskdefs/${2##*/}.json"
}

# write_ecr_tag <tmpdir> <tag> <digest>
write_ecr_tag() {
    mkdir -p "$1/ecr"
    printf '%s' "$3" > "$1/ecr/$2"
}

# Our :808 (sha tag) and terraform's :809 (latest tag). latest's digest
# decides whether :809 carries our image.
setup_incident_taskdefs() {
    local tmpdir="$1" latest_digest="$2"
    write_taskdef "$tmpdir" "$OUR_TD" "${ECR_REPO_URI}:c05deff"
    write_taskdef "$tmpdir" "$TF_TD" "${ECR_REPO_URI}:latest"
    write_ecr_tag "$tmpdir" "c05deff" "$OUR_DIGEST"
    write_ecr_tag "$tmpdir" "latest" "$latest_digest"
}

describe_calls() {
    grep -c "ecs describe-services" "$1/calls.log" || true
}

# Test 15: superseded by a newer revision carrying our image → follow it and
# exit 0 when it completes. This is the PR #5070 race.
test_superseded_same_image_follows_and_succeeds() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    setup_incident_taskdefs "$tmpdir" "$OUR_DIGEST"
    write_services "$tmpdir/responses/01.json" \
        "[$(dep_json ecs-svc/808 "$OUR_TD" PRIMARY IN_PROGRESS 0 1)]"
    write_services "$tmpdir/responses/02.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY IN_PROGRESS 0 1)]"
    write_services "$tmpdir/responses/03.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY COMPLETED 1 1)]"

    local output exit_code=0
    output=$(RUN_TASK_DEF_ARN="$OUR_TD" run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "superseded by same image: exits 0 once the superseding deployment completes"
    else
        fail "superseded by same image: exits 0 once the superseding deployment completes" "exit=$exit_code output=$output"
    fi
    if grep -q "was superseded by deployment ecs-svc/809" <<< "$output" \
        && grep -q "carries this run's image" <<< "$output"; then
        pass "superseded by same image: names the superseding deployment and the image match"
    else
        fail "superseded by same image: names the superseding deployment and the image match" "output=$output"
    fi
    if grep -q "Superseding deployment ecs-svc/809 .* completed" <<< "$output"; then
        pass "superseded by same image: success line names the followed deployment"
    else
        fail "superseded by same image: success line names the followed deployment" "output=$output"
    fi
    if grep -q "not yet visible" <<< "$output"; then
        fail "superseded by same image: never reports 'not yet visible'" "output=$output"
    else
        pass "superseded by same image: never reports 'not yet visible'"
    fi
}

# Test 16: superseded by a deployment running an OLDER image → exit 3 with
# the reason, promptly (no waiting out the timeout).
test_superseded_older_image_fails_clearly() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    setup_incident_taskdefs "$tmpdir" "$OLD_DIGEST"
    write_services "$tmpdir/responses/01.json" \
        "[$(dep_json ecs-svc/808 "$OUR_TD" PRIMARY IN_PROGRESS 0 1)]"
    write_services "$tmpdir/responses/02.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY IN_PROGRESS 0 1)]"

    local output exit_code=0 t0=$SECONDS
    output=$(RUN_TASK_DEF_ARN="$OUR_TD" ROLLOUT_TIMEOUT_SECS=30 run_script "$tmpdir" 2>&1) || exit_code=$?
    local took=$((SECONDS - t0))

    if [[ "$exit_code" -eq 3 ]]; then
        pass "superseded by older image: exits 3"
    else
        fail "superseded by older image: exits 3" "exit=$exit_code output=$output"
    fi
    if grep -q "SUPERSEDED: .* does not carry this run's image" <<< "$output" \
        && grep -q "$OLD_DIGEST" <<< "$output" && grep -q "$OUR_DIGEST" <<< "$output"; then
        pass "superseded by older image: says why, with both digests"
    else
        fail "superseded by older image: says why, with both digests" "output=$output"
    fi
    # Exit 3 (not 124) already shows it did not wait out the timeout; this
    # bound only has to tell a ~2s run from the 30s budget (#5083).
    if [[ "$took" -lt 20 ]]; then
        pass "superseded by older image: fails promptly (${took}s, budget 30s)"
    else
        fail "superseded by older image: fails promptly" "took ${took}s"
    fi
}

# Test 17: AC for #5073 — our deployment was never seen in this call (e.g.
# a re-run after exit 124) and a newer PRIMARY revision is present. The
# verdict comes on the FIRST poll, not after a grace period or the timeout.
test_superseded_newer_revision_detected_on_first_poll() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    setup_incident_taskdefs "$tmpdir" "$OUR_DIGEST"
    write_services "$tmpdir/responses/01.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY COMPLETED 1 1)]"

    local output exit_code=0
    output=$(RUN_TASK_DEF_ARN="$OUR_TD" ROLLOUT_TIMEOUT_SECS=60 run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "newer PRIMARY revision, ours never seen: exits 0"
    else
        fail "newer PRIMARY revision, ours never seen: exits 0" "exit=$exit_code output=$output"
    fi
    if grep -q "newer revision of our family" <<< "$output" && [[ "$(describe_calls "$tmpdir")" -eq 1 ]]; then
        pass "newer PRIMARY revision, ours never seen: superseded verdict on the first poll"
    else
        fail "newer PRIMARY revision, ours never seen: superseded verdict on the first poll" \
            "describe calls=$(describe_calls "$tmpdir") output=$output"
    fi
}

# Test 18: our deployment is still listed but DRAINING with COMPLETED 0/0
# (what ECS shows for a replaced deployment). That must NOT read as a
# scale-to-zero success; with a different image on the PRIMARY it is exit 3.
test_demoted_deployment_is_not_success() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    setup_incident_taskdefs "$tmpdir" "$OLD_DIGEST"
    write_services "$tmpdir/responses/01.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY IN_PROGRESS 1 1), $(dep_json ecs-svc/808 "$OUR_TD" DRAINING COMPLETED 0 0)]"

    local output exit_code=0
    output=$(RUN_TASK_DEF_ARN="$OUR_TD" run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 3 ]]; then
        pass "demoted (DRAINING COMPLETED 0/0) deployment with another image: exits 3, not 0"
    else
        fail "demoted (DRAINING COMPLETED 0/0) deployment with another image: exits 3, not 0" "exit=$exit_code output=$output"
    fi
    if grep -q "our deployment is now DRAINING" <<< "$output"; then
        pass "demoted deployment: names the DRAINING status"
    else
        fail "demoted deployment: names the DRAINING status" "output=$output"
    fi
}

# Test 19: no revision signal (ARNs without revisions) — superseded only
# after ROLLOUT_SUPERSEDE_GRACE_POLLS missing polls, then followed when the
# image strings are identical (no ECR lookup needed).
test_superseded_after_grace_polls() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_taskdef "$tmpdir" "arn:test:new" "${ECR_REPO_URI}:abc1234"
    write_taskdef "$tmpdir" "arn:test:other" "${ECR_REPO_URI}:abc1234"
    write_services "$tmpdir/responses/01.json" \
        "[$(dep_json ecs-svc/other arn:test:other PRIMARY IN_PROGRESS 0 1)]"
    write_services "$tmpdir/responses/04.json" \
        "[$(dep_json ecs-svc/other arn:test:other PRIMARY COMPLETED 1 1)]"
    cp "$tmpdir/responses/01.json" "$tmpdir/responses/02.json"
    cp "$tmpdir/responses/01.json" "$tmpdir/responses/03.json"

    local output exit_code=0
    output=$(ROLLOUT_SUPERSEDE_GRACE_POLLS=3 run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "grace-poll supersession with identical image: exits 0"
    else
        fail "grace-poll supersession with identical image: exits 0" "exit=$exit_code output=$output"
    fi
    if [[ "$(grep -c "not yet visible" <<< "$output")" -eq 2 ]] \
        && grep -q "missing for 3 polls" <<< "$output"; then
        pass "grace-poll supersession: 2 'not yet visible' polls, superseded on the 3rd"
    else
        fail "grace-poll supersession: 2 'not yet visible' polls, superseded on the 3rd" "output=$output"
    fi
    if grep -q "ecr describe-images" "$tmpdir/calls.log"; then
        fail "grace-poll supersession: identical image refs skip the ECR lookup" "calls=$(cat "$tmpdir/calls.log")"
    else
        pass "grace-poll supersession: identical image refs skip the ECR lookup"
    fi
}

# Test 20: budget — the superseding deployment never finishes. Exit 124
# (still rolling out, #4835) and say how to keep waiting on it.
test_superseded_follow_times_out_124() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    setup_incident_taskdefs "$tmpdir" "$OUR_DIGEST"
    write_services "$tmpdir/responses/01.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY IN_PROGRESS 0 1)]"

    local output exit_code=0
    output=$(RUN_TASK_DEF_ARN="$OUR_TD" ROLLOUT_TIMEOUT_SECS=1 run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 124 ]]; then
        pass "following a superseding deployment that never completes: exits 124"
    else
        fail "following a superseding deployment that never completes: exits 124" "exit=$exit_code output=$output"
    fi
    if grep -q "NEW_DEPLOYMENT_ID=ecs-svc/809" <<< "$output"; then
        pass "timeout while following: prints how to wait on the superseding deployment"
    else
        fail "timeout while following: prints how to wait on the superseding deployment" "output=$output"
    fi
}

# Test 20b (#5097): deployment-id mode. Call 1 follows a same-image
# superseding deployment and times out (124). ECS then drops the original
# deployment. The resume must name the FOLLOWED deployment, and resuming on
# it exits 0 — not 3 ("superseded before this call saw it").
test_deployment_id_resume_after_follow_does_not_exit_3() {
    local tmpdir followed_file
    tmpdir=$(make_temp_dir)
    followed_file="$tmpdir/followed-id"
    setup_incident_taskdefs "$tmpdir" "$OUR_DIGEST"
    # Poll 1: terraform's :809 is PRIMARY, ours (:808) is still listed but
    # DRAINING. A 0s budget ends the call after this one poll.
    write_services "$tmpdir/responses/01.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY IN_PROGRESS 0 1), $(dep_json ecs-svc/808 "$OUR_TD" DRAINING IN_PROGRESS 1 0)]"

    local output exit_code=0
    output=$(ROLLOUT_FOLLOWED_ID_FILE="$followed_file" ROLLOUT_TIMEOUT_SECS=0 \
        run_script_by_deployment_id "$tmpdir" ecs-svc/808 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 124 ]] && grep -q "Waiting on it instead" <<< "$output"; then
        pass "deployment-id follow: first call follows the same-image deployment and exits 124"
    else
        fail "deployment-id follow: first call follows the same-image deployment and exits 124" "exit=$exit_code output=$output"
    fi
    local followed=""
    read -r followed < "$followed_file" || true
    if [[ "$followed" == "ecs-svc/809" ]]; then
        pass "deployment-id follow: writes the followed deployment ID to ROLLOUT_FOLLOWED_ID_FILE"
    else
        fail "deployment-id follow: writes the followed deployment ID to ROLLOUT_FOLLOWED_ID_FILE" "file=$followed"
    fi
    if grep -q "Re-run with NEW_DEPLOYMENT_ID=ecs-svc/809" <<< "$output" \
        && ! grep -q "Re-run with the same env vars" <<< "$output"; then
        pass "deployment-id follow: resume hint names the followed deployment, not the original"
    else
        fail "deployment-id follow: resume hint names the followed deployment, not the original" "output=$output"
    fi

    # Resume: the original :808 deployment is gone from deployments[].
    local tmpdir2
    tmpdir2=$(make_temp_dir)
    setup_incident_taskdefs "$tmpdir2" "$OUR_DIGEST"
    write_services "$tmpdir2/responses/01.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY IN_PROGRESS 0 1)]"
    write_services "$tmpdir2/responses/02.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY COMPLETED 1 1)]"

    exit_code=0
    output=$(ROLLOUT_TIMEOUT_SECS=60 run_script_by_deployment_id "$tmpdir2" "$followed" 2>&1) || exit_code=$?
    if [[ "$exit_code" -eq 0 ]] && ! grep -q "SUPERSEDED" <<< "$output"; then
        pass "deployment-id resume on the followed deployment (original gone): exits 0, not 3"
    else
        fail "deployment-id resume on the followed deployment (original gone): exits 0, not 3" "exit=$exit_code output=$output"
    fi
}

# Test 20c: nothing followed → ROLLOUT_FOLLOWED_ID_FILE stays empty.
test_followed_id_file_untouched_without_follow() {
    local tmpdir followed_file
    tmpdir=$(make_temp_dir)
    followed_file="$tmpdir/followed-id"
    : > "$followed_file"
    write_response "$tmpdir/responses/01.json" COMPLETED 1 1 0 arn:test:shared ecs-svc/new-redeploy

    local exit_code=0
    ROLLOUT_FOLLOWED_ID_FILE="$followed_file" run_script_by_deployment_id "$tmpdir" ecs-svc/new-redeploy \
        > /dev/null 2>&1 || exit_code=$?
    if [[ "$exit_code" -eq 0 && ! -s "$followed_file" ]]; then
        pass "no supersession: ROLLOUT_FOLLOWED_ID_FILE left empty"
    else
        fail "no supersession: ROLLOUT_FOLLOWED_ID_FILE left empty" "exit=$exit_code file=$(cat "$followed_file")"
    fi
}

# Test 21: the ECR lookup fails outright (expired credentials) → exit 125,
# state unknown — not a superseded verdict and not a timeout.
test_superseded_image_lookup_failure_125() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    setup_incident_taskdefs "$tmpdir" "$OUR_DIGEST"
    write_services "$tmpdir/responses/01.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY IN_PROGRESS 0 1)]"

    local output exit_code=0
    output=$(ECR_FAIL=1 RUN_TASK_DEF_ARN="$OUR_TD" run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 125 ]]; then
        pass "ECR lookup failure during the image check: exits 125"
    else
        fail "ECR lookup failure during the image check: exits 125" "exit=$exit_code output=$output"
    fi
}

# Test 22: deleted tag (ImageNotFoundException) is an answer, not an
# unknown state: the images cannot be matched → exit 3.
test_superseded_deleted_tag_is_mismatch() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    setup_incident_taskdefs "$tmpdir" "$OUR_DIGEST"
    rm -f "$tmpdir/ecr/latest"
    write_services "$tmpdir/responses/01.json" \
        "[$(dep_json ecs-svc/809 "$TF_TD" PRIMARY IN_PROGRESS 0 1)]"

    local output exit_code=0
    output=$(RUN_TASK_DEF_ARN="$OUR_TD" run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 3 ]] && grep -q "could not resolve both images" <<< "$output"; then
        pass "deleted tag on the superseding image: exits 3 with reason"
    else
        fail "deleted tag on the superseding image: exits 3 with reason" "exit=$exit_code output=$output"
    fi
}

# Test 23: normal rollout is unchanged — no task-def or ECR lookups at all.
test_normal_rollout_makes_no_image_lookups() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_services "$tmpdir/responses/01.json" \
        "[$(dep_json ecs-svc/808 "$OUR_TD" PRIMARY IN_PROGRESS 0 1), $(dep_json ecs-svc/807 "${TD_FAMILY_ARN}:807" ACTIVE COMPLETED 1 1)]"
    write_services "$tmpdir/responses/02.json" \
        "[$(dep_json ecs-svc/808 "$OUR_TD" PRIMARY COMPLETED 1 1)]"

    local output exit_code=0
    output=$(RUN_TASK_DEF_ARN="$OUR_TD" run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]] && grep -q "Service updated successfully" <<< "$output"; then
        pass "normal rollout with an older ACTIVE deployment: exits 0"
    else
        fail "normal rollout with an older ACTIVE deployment: exits 0" "exit=$exit_code output=$output"
    fi
    if grep -qE "describe-task-definition|ecr describe-images" "$tmpdir/calls.log" \
        || grep -q "superseded" <<< "$output"; then
        fail "normal rollout: no supersession logic fires" "output=$output calls=$(cat "$tmpdir/calls.log")"
    else
        pass "normal rollout: no supersession logic fires"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_immediate_completed
test_in_progress_then_completed
test_deployment_missing_then_appears
test_failed_rollout
test_timeout
test_scale_to_zero_completed
test_deployment_id_scale_to_zero_completed
test_completed_but_running_not_yet_matching
test_aws_cli_failure
test_missing_required_env
test_missing_selector_env
test_both_selectors_set
test_deployment_id_immediate_completed
test_deployment_id_disambiguates_shared_taskdef
test_deployment_id_failed_rollout
test_deployment_id_timeout
test_superseded_same_image_follows_and_succeeds
test_superseded_older_image_fails_clearly
test_superseded_newer_revision_detected_on_first_poll
test_demoted_deployment_is_not_success
test_superseded_after_grace_polls
test_superseded_follow_times_out_124
test_deployment_id_resume_after_follow_does_not_exit_3
test_followed_id_file_untouched_without_follow
test_superseded_image_lookup_failure_125
test_superseded_deleted_tag_is_mismatch
test_normal_rollout_makes_no_image_lookups

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
