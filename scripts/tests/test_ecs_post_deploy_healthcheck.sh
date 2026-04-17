#!/usr/bin/env bash
# test_ecs_post_deploy_healthcheck.sh — Unit tests for
# scripts/ecs-post-deploy-healthcheck.sh.
#
# Modeled on scripts/tests/test_wait_for_rollout.sh: exercises the script
# against a mock `aws` CLI that emits pre-canned JSON responses for each
# AWS subcommand the script calls (describe-services, list-tasks,
# describe-tasks, describe-log-streams, get-log-events).
#
# Covers:
#   - scale-to-zero success (desired=0, running=0) — the #2605 case
#   - running==desired>0 success
#   - running<desired failure (service-check)
#   - crash-loop detection (stopped container with nonzero exit code)
#   - log-check warning (critical errors in recent logs) stays non-fatal
#   - log-check failure (empty stream / CloudWatch unavailable) stays non-fatal
#   - missing required env vars exits non-zero
#
# Usage:
#   scripts/tests/test_ecs_post_deploy_healthcheck.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_UNDER_TEST="$REPO_ROOT/scripts/ecs-post-deploy-healthcheck.sh"

if [[ ! -x "$SCRIPT_UNDER_TEST" ]]; then
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

# Create a mock `aws` CLI that dispatches on the subcommand and emits a
# pre-canned JSON file from <tmpdir>/<cmd>.json (or <cmd>.txt for the
# describe-log-streams --query + --output text case).
#
# Supported dispatch keys:
#   ecs describe-services      → describe-services.json
#   ecs list-tasks             → list-tasks.json
#   ecs describe-tasks         → describe-tasks.json
#   logs describe-log-streams  → describe-log-streams.txt  (plain text)
#   logs get-log-events        → get-log-events.json
#
# If a dispatch file does not exist, the mock exits 1 (lets us exercise the
# "CloudWatch unavailable" path by simply not providing describe-log-streams).
setup_mock_aws() {
    local tmpdir="$1"
    local mock="$tmpdir/aws"

    cat > "$mock" << 'MOCK'
#!/usr/bin/env bash
# Mock aws CLI — dispatches on subcommand.
set -euo pipefail

RESPONSES_DIR="${RESPONSES_DIR:?RESPONSES_DIR required}"
CALL_LOG="${CALL_LOG:-/dev/null}"

echo "$*" >> "$CALL_LOG"

case "$*" in
    "ecs describe-services"*)
        file="$RESPONSES_DIR/describe-services.json"
        ;;
    "ecs list-tasks"*)
        file="$RESPONSES_DIR/list-tasks.json"
        ;;
    "ecs describe-tasks"*)
        file="$RESPONSES_DIR/describe-tasks.json"
        ;;
    "logs describe-log-streams"*)
        file="$RESPONSES_DIR/describe-log-streams.txt"
        ;;
    "logs get-log-events"*)
        file="$RESPONSES_DIR/get-log-events.json"
        ;;
    *)
        echo "MOCK ERROR: unknown aws invocation: $*" >&2
        exit 1
        ;;
esac

if [[ ! -f "$file" ]]; then
    # Emulate the aws CLI failing for this subcommand. The script under test
    # handles logs errors gracefully (|| echo '...') and ecs errors by exit 1.
    echo "MOCK: no fixture for $* (file=$file)" >&2
    exit 1
fi

cat "$file"
MOCK
    chmod +x "$mock"
    echo "$mock"
}

# Write a describe-services response with the given counts.
# Usage: write_describe_services <tmpdir> <running> <desired>
write_describe_services() {
    local tmpdir="$1"
    local running="$2"
    local desired="$3"
    cat > "$tmpdir/describe-services.json" << JSON
{
  "serviceName": "test-svc",
  "desiredCount": $desired,
  "runningCount": $running,
  "pendingCount": 0
}
JSON
}

# Write a list-tasks response with N task ARNs.
# Usage: write_list_tasks <tmpdir> <count>
write_list_tasks() {
    local tmpdir="$1"
    local count="$2"
    local arns="[]"
    if [[ "$count" -gt 0 ]]; then
        arns='["arn:aws:ecs:us-west-2:123:task/test-cluster/abc123"]'
    fi
    cat > "$tmpdir/list-tasks.json" << JSON
$arns
JSON
}

# Write a describe-tasks response.
# Usage: write_describe_tasks <tmpdir> <stopped_with_nonzero_count>
write_describe_tasks() {
    local tmpdir="$1"
    local stopped_count="${2:-0}"
    # Always include a container named 'ingestion-worker'. If stopped_count>0,
    # add an extra STOPPED container with exitCode=1.
    local containers='[{"name":"ingestion-worker","lastStatus":"RUNNING","exitCode":null,"reason":null}]'
    if [[ "$stopped_count" -gt 0 ]]; then
        containers='[{"name":"ingestion-worker","lastStatus":"RUNNING","exitCode":null,"reason":null},{"name":"sidecar","lastStatus":"STOPPED","exitCode":1,"reason":"Exited"}]'
    fi
    cat > "$tmpdir/describe-tasks.json" << JSON
{
  "lastStatus": "RUNNING",
  "healthStatus": "HEALTHY",
  "containers": $containers
}
JSON
}

# Write a describe-log-streams response (plain text — aws CLI --query +
# --output text returns just the log stream name).
# Usage: write_describe_log_streams <tmpdir> <stream_name_or_None>
write_describe_log_streams() {
    local tmpdir="$1"
    local name="${2:-test-stream}"
    printf '%s\n' "$name" > "$tmpdir/describe-log-streams.txt"
}

# Write a get-log-events response.
# Usage: write_get_log_events <tmpdir> <critical_count>
#   critical_count=0 → only benign INFO lines
#   critical_count>N → N lines matching CRITICAL / Traceback / crash
write_get_log_events() {
    local tmpdir="$1"
    local critical_count="${2:-0}"
    local events='[{"message":"INFO starting worker","timestamp":1}]'
    if [[ "$critical_count" -gt 0 ]]; then
        events='[{"message":"INFO starting worker","timestamp":1},{"message":"CRITICAL failure occurred","timestamp":2},{"message":"Traceback (most recent call last):","timestamp":3}]'
    fi
    cat > "$tmpdir/get-log-events.json" << JSON
{"events": $events}
JSON
}

# Run the script under test with mocks wired up.
run_script() {
    local tmpdir="$1"
    local mock_aws
    mock_aws=$(setup_mock_aws "$tmpdir")

    AWS_CLI="$mock_aws" \
    RESPONSES_DIR="$tmpdir" \
    CALL_LOG="$tmpdir/calls.log" \
    ECS_CLUSTER="test-cluster" \
    INGESTION_SERVICE="test-svc" \
    LOG_GROUP="/ecs/test-svc" \
        "$SCRIPT_UNDER_TEST"
}

# ── Tests ──────────────────────────────────────────────────────────────────

# Test 1: scale-to-zero success (desired=0, running=0).
# Reproduces the #2605 case: the dev ingestion worker runs at desiredCount=0
# and should be treated as healthy when running=0.
test_scale_to_zero_success() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 0 0
    # No list-tasks / describe-tasks needed — crash-check is skipped when
    # desiredCount == 0. Still need log-check fixtures so the log step
    # succeeds (or at least doesn't error fatally).
    write_describe_log_streams "$tmpdir" "test-stream"
    write_get_log_events "$tmpdir" 0

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "scale-to-zero (desired=0, running=0) exits 0"
    else
        fail "scale-to-zero (desired=0, running=0) exits 0" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "scaled to zero"; then
        pass "scale-to-zero emits 'scaled to zero' message"
    else
        fail "scale-to-zero emits 'scaled to zero' message" "output=$output"
    fi

    if echo "$output" | grep -q "skipping crash-loop check"; then
        pass "scale-to-zero skips crash-loop check"
    else
        fail "scale-to-zero skips crash-loop check" "output=$output"
    fi
}

# Test 2: running==desired>0 success (normal healthy rollout).
test_running_equals_desired_success() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 1 1
    write_list_tasks "$tmpdir" 1
    write_describe_tasks "$tmpdir" 0
    write_describe_log_streams "$tmpdir" "test-stream"
    write_get_log_events "$tmpdir" 0

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "running==desired>0 exits 0"
    else
        fail "running==desired>0 exits 0" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "Service has expected number of running tasks"; then
        pass "running==desired>0 emits expected-count message"
    else
        fail "running==desired>0 emits expected-count message" "output=$output"
    fi

    if echo "$output" | grep -q "No crash-loop detected"; then
        pass "running==desired>0 passes crash-loop check"
    else
        fail "running==desired>0 passes crash-loop check" "output=$output"
    fi
}

# Test 3: running<desired failure (service-check rejects).
test_running_below_desired_failure() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 0 1

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "running<desired exits 1"
    else
        fail "running<desired exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "does not match desired"; then
        pass "running<desired emits mismatch error"
    else
        fail "running<desired emits mismatch error" "output=$output"
    fi
}

# Test 4: crash-loop detected (stopped container with nonzero exit code).
test_crash_loop_detected() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 1 1
    write_list_tasks "$tmpdir" 1
    write_describe_tasks "$tmpdir" 1  # 1 stopped container w/ exit=1
    # log-check fixtures omitted — script should exit before getting there.

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "crash-loop exits 1"
    else
        fail "crash-loop exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "possible crash-loop"; then
        pass "crash-loop emits clear error"
    else
        fail "crash-loop emits clear error" "output=$output"
    fi
}

# Test 5: crash-check with zero running tasks when desired>0 fails.
test_no_running_tasks_failure() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    # Service reports running==desired==1 at service-check time (stale read),
    # but list-tasks returns empty — crash-check must fail cleanly.
    write_describe_services "$tmpdir" 1 1
    write_list_tasks "$tmpdir" 0

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "no running tasks with desired>0 exits 1"
    else
        fail "no running tasks with desired>0 exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "No running tasks found"; then
        pass "no running tasks emits clear error"
    else
        fail "no running tasks emits clear error" "output=$output"
    fi
}

# Test 6: log-check warning — critical errors in logs, but deploy stays green.
# Log noise must NOT fail the whole deploy (per inline comment in the
# original workflow: "Warning only — don't fail the whole deploy for log noise").
test_log_check_warning_only() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 1 1
    write_list_tasks "$tmpdir" 1
    write_describe_tasks "$tmpdir" 0
    write_describe_log_streams "$tmpdir" "test-stream"
    # Critical errors present in recent logs:
    write_get_log_events "$tmpdir" 2

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "critical errors in logs stay warning-only (exit 0)"
    else
        fail "critical errors in logs stay warning-only (exit 0)" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "WARNING: Found .* critical error indicators"; then
        pass "log-check emits warning when critical errors present"
    else
        fail "log-check emits warning when critical errors present" "output=$output"
    fi
}

# Test 7: log-check with no log stream available — warning, not failure.
test_log_check_no_stream_warning() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 1 1
    write_list_tasks "$tmpdir" 1
    write_describe_tasks "$tmpdir" 0
    # describe-log-streams fixture returns "None" (aws CLI text output for
    # empty result). No get-log-events fixture — the script bails out
    # before calling it.
    write_describe_log_streams "$tmpdir" "None"

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "no log stream stays warning-only (exit 0)"
    else
        fail "no log stream stays warning-only (exit 0)" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "No log streams found"; then
        pass "no log stream emits 'No log streams found' warning"
    else
        fail "no log stream emits 'No log streams found' warning" "output=$output"
    fi
}

# Test 8: log-check when describe-log-streams itself fails (CloudWatch
# unavailable) stays warning-only, not fatal. The script relies on `2>/dev/null
# || echo ""` redirection — so the downstream branch treats missing data as
# "no stream", which is a warning.
test_log_check_cloudwatch_unavailable() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 1 1
    write_list_tasks "$tmpdir" 1
    write_describe_tasks "$tmpdir" 0
    # Intentionally do not create describe-log-streams.txt — the mock will
    # exit 1, which the script handles with `|| echo ""`.

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "CloudWatch describe-log-streams failure stays warning-only"
    else
        fail "CloudWatch describe-log-streams failure stays warning-only" "exit=$exit_code output=$output"
    fi
}

# Test 9: Missing required env vars exits non-zero.
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

# Test 10: describe-services failure (aws CLI errors) exits 1.
test_describe_services_failure() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    # Intentionally do not create describe-services.json — the mock will
    # exit 1 on the first call.

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "describe-services failure exits 1"
    else
        fail "describe-services failure exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "describe-services failed"; then
        pass "describe-services failure emits clear error"
    else
        fail "describe-services failure emits clear error" "output=$output"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_scale_to_zero_success
test_running_equals_desired_success
test_running_below_desired_failure
test_crash_loop_detected
test_no_running_tasks_failure
test_log_check_warning_only
test_log_check_no_stream_warning
test_log_check_cloudwatch_unavailable
test_missing_required_env
test_describe_services_failure

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
