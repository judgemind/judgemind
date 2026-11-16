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
#   - #5102 polling: a transient 0/1 during a task replacement, then 1/1 with
#     the PRIMARY deployment COMPLETED, passes; a task of the PRIMARY
#     deployment that crashed fails with its stoppedReason and exit code;
#     an old task stopped by the deployment is not a crash; rolloutState
#     FAILED fails; a service that never settles exits 124 (budget
#     exhausted); describe-services failing exits 125.
#
# The service-check wait runs on a fake clock (#5083): the mock aws moves
# WAIT_BUDGET_CLOCK_FILE forward on each describe-services call and the poll
# interval is 0, so the 480s budget is exercised without wall-clock time.
#
# Usage:
#   scripts/tests/test_ecs_post_deploy_healthcheck.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)
. "$(dirname "${BASH_SOURCE[0]}")/_git-env-scrub.sh"  # no writes to the real repo (#5058)

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
        # Fake clock (#5083): each poll moves time forward.
        if [[ -n "${WAIT_BUDGET_CLOCK_FILE:-}" ]]; then
            read -r now < "$WAIT_BUDGET_CLOCK_FILE"
            echo $((now + ${MOCK_CLOCK_STEP:-10})) > "$WAIT_BUDGET_CLOCK_FILE"
        fi
        # Sequence: describe-services.<n>.json answers the n-th call when
        # present; describe-services.json answers every other call.
        n=$(( $(cat "$RESPONSES_DIR/.ds-count" 2>/dev/null || echo 0) + 1 ))
        echo "$n" > "$RESPONSES_DIR/.ds-count"
        file="$RESPONSES_DIR/describe-services.$n.json"
        if [[ ! -f "$file" ]]; then
            file="$RESPONSES_DIR/describe-services.json"
        fi
        ;;
    "ecs list-tasks"*"--desired-status STOPPED"*)
        file="$RESPONSES_DIR/list-tasks-stopped.json"
        if [[ ! -f "$file" ]]; then
            echo "[]"
            exit 0
        fi
        ;;
    "ecs list-tasks"*)
        file="$RESPONSES_DIR/list-tasks.json"
        ;;
    "ecs describe-tasks"*"--query tasks[0]"*)
        file="$RESPONSES_DIR/describe-tasks.json"
        ;;
    "ecs describe-tasks"*)
        file="$RESPONSES_DIR/describe-tasks-stopped.json"
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
# Usage: write_describe_services <tmpdir> <running> <desired> [rollout] [n]
#   rollout — omitted: no deployments listed (the pre-#5102 fixture shape).
#             COMPLETED / FAILED: one PRIMARY deployment in that state.
#             IN_PROGRESS: a PRIMARY deployment rolling out plus the ACTIVE
#             deployment it replaces.
#   n       — write describe-services.<n>.json (answers only the n-th call)
#             instead of describe-services.json (answers every other call).
write_describe_services() {
    local tmpdir="$1"
    local running="$2"
    local desired="$3"
    local rollout="${4:-}"
    local n="${5:-}"
    local file="$tmpdir/describe-services.json"
    if [[ -n "$n" ]]; then
        file="$tmpdir/describe-services.$n.json"
    fi
    local deployments=""
    case "$rollout" in
        "") ;;
        IN_PROGRESS)
            deployments=',"deployments":[{"id":"ecs-svc/new","status":"PRIMARY","taskDefinition":"arn:aws:ecs:us-west-2:123:task-definition/test-svc:2","rolloutState":"IN_PROGRESS","rolloutStateReason":"ECS deployment ecs-svc/new in progress.","runningCount":'"$running"',"desiredCount":'"$desired"'},{"id":"ecs-svc/old","status":"ACTIVE","taskDefinition":"arn:aws:ecs:us-west-2:123:task-definition/test-svc:1","rolloutState":"COMPLETED","runningCount":0,"desiredCount":0}]'
            ;;
        *)
            deployments=',"deployments":[{"id":"ecs-svc/new","status":"PRIMARY","taskDefinition":"arn:aws:ecs:us-west-2:123:task-definition/test-svc:2","rolloutState":"'"$rollout"'","rolloutStateReason":"ECS deployment ecs-svc/new '"$rollout"'.","runningCount":'"$running"',"desiredCount":'"$desired"'}]'
            ;;
    esac
    cat > "$file" << JSON
{
  "serviceName": "test-svc",
  "desiredCount": $desired,
  "runningCount": $running,
  "pendingCount": 0$deployments
}
JSON
}

# Write the stopped-task fixtures: list-tasks --desired-status STOPPED and
# the describe-tasks answer for those ARNs.
# Usage: write_stopped_tasks <tmpdir> <kind>...
#   replaced — an old-deployment task the deployment stopped (exit 137 after
#              stopTimeout: still a normal replacement, not a crash)
#   crashed  — a PRIMARY-deployment task whose essential container exited 1
write_stopped_tasks() {
    local tmpdir="$1"
    shift
    local arns="" tasks="" sep=""
    local kind
    for kind in "$@"; do
        case "$kind" in
            replaced)
                arns="${arns}${sep}\"arn:aws:ecs:us-west-2:123:task/test-cluster/oldtask\""
                tasks="${tasks}${sep}"'{"taskArn":"arn:aws:ecs:us-west-2:123:task/test-cluster/oldtask","lastStatus":"STOPPED","startedBy":"ecs-svc/old","taskDefinitionArn":"arn:aws:ecs:us-west-2:123:task-definition/test-svc:1","stoppedReason":"Scaling activity initiated by (deployment ecs-svc/new)","stopCode":"ServiceSchedulerInitiated","containers":[{"name":"ingestion-worker","lastStatus":"STOPPED","exitCode":137}]}'
                ;;
            crashed)
                arns="${arns}${sep}\"arn:aws:ecs:us-west-2:123:task/test-cluster/newtask\""
                tasks="${tasks}${sep}"'{"taskArn":"arn:aws:ecs:us-west-2:123:task/test-cluster/newtask","lastStatus":"STOPPED","startedBy":"ecs-svc/new","taskDefinitionArn":"arn:aws:ecs:us-west-2:123:task-definition/test-svc:2","stoppedReason":"Essential container in task exited","stopCode":"EssentialContainerExited","containers":[{"name":"ingestion-worker","lastStatus":"STOPPED","exitCode":1,"reason":"ModuleNotFoundError"}]}'
                ;;
        esac
        sep=","
    done
    echo "[$arns]" > "$tmpdir/list-tasks-stopped.json"
    echo "[$tasks]" > "$tmpdir/describe-tasks-stopped.json"
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
    # Fake clock well above the real $SECONDS (see _wait_budget_lib.sh).
    echo 100000 > "$tmpdir/clock"

    AWS_CLI="$mock_aws" \
    RESPONSES_DIR="$tmpdir" \
    CALL_LOG="$tmpdir/calls.log" \
    WAIT_BUDGET_CLOCK_FILE="$tmpdir/clock" \
    WAIT_BUDGET_NO_CAFFEINATE=1 \
    HEALTHCHECK_POLL_INTERVAL=0 \
    MOCK_CLOCK_STEP="${MOCK_CLOCK_STEP:-10}" \
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

# Test 3: running<desired that never settles exhausts the 480s budget and
# exits 124 (still rolling out), after polling rather than reading once.
test_running_below_desired_failure() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 0 1 IN_PROGRESS

    local output exit_code
    exit_code=0
    output=$(MOCK_CLOCK_STEP=100 run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 124 ]]; then
        pass "running<desired that never settles exits 124 (budget exhausted)"
    else
        fail "running<desired that never settles exits 124 (budget exhausted)" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "Timed out after 480s" \
        && echo "$output" | grep -q "does not match desired"; then
        pass "budget exhaustion names the 480s budget and the count mismatch"
    else
        fail "budget exhaustion names the 480s budget and the count mismatch" "output=$output"
    fi

    local polls
    polls=$(grep -c "^ecs describe-services" "$tmpdir/calls.log" || true)
    if [[ "$polls" -ge 4 && "$polls" -le 6 ]]; then
        pass "budget exhaustion polled ${polls} times on the fake clock (100s per poll, 480s budget)"
    else
        fail "budget exhaustion polls 4-6 times on the fake clock" "polls=$polls output=$output"
    fi
}

# Test 3b (#5102): the PR #5100 case. The replacement briefly shows 0/1
# while the PRIMARY deployment is IN_PROGRESS, then 1/1 COMPLETED → pass.
test_transient_zero_during_replacement_passes() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 0 1 IN_PROGRESS 1
    write_describe_services "$tmpdir" 0 1 IN_PROGRESS 2
    # Counts match but the rollout is not finished yet: keep waiting.
    write_describe_services "$tmpdir" 1 1 IN_PROGRESS 3
    write_describe_services "$tmpdir" 1 1 COMPLETED
    # The old task the deployment stopped is listed, as on the real service.
    write_stopped_tasks "$tmpdir" replaced
    write_list_tasks "$tmpdir" 1
    write_describe_tasks "$tmpdir" 0
    write_describe_log_streams "$tmpdir" "test-stream"
    write_get_log_events "$tmpdir" 0

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        pass "transient 0/1 during replacement then 1/1 COMPLETED exits 0"
    else
        fail "transient 0/1 during replacement then 1/1 COMPLETED exits 0" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "poll 1: running=0 desired=1 .*rolloutState=IN_PROGRESS" \
        && echo "$output" | grep -q "poll 4: running=1 desired=1 .*rolloutState=COMPLETED" \
        && echo "$output" | grep -q "Service is steady after 40s (poll 4)"; then
        pass "transient replacement logs each poll and becomes steady on poll 4"
    else
        fail "transient replacement logs each poll and becomes steady on poll 4" "output=$output"
    fi

    if echo "$output" | grep -q "CRASHED"; then
        fail "a task stopped by the deployment is not reported as a crash" "output=$output"
    else
        pass "a task stopped by the deployment is not reported as a crash"
    fi

    if echo "$output" | grep -q "No crash-loop detected"; then
        pass "transient replacement still runs the crash-loop check after settling"
    else
        fail "transient replacement still runs the crash-loop check after settling" "output=$output"
    fi
}

# Test 3c (#5102): a task of the PRIMARY deployment crashed → fail at once
# with the reason, even though the old task was also stopped normally.
test_crashed_task_fails_with_reason() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 0 1 IN_PROGRESS
    write_stopped_tasks "$tmpdir" replaced crashed

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]]; then
        pass "crashed PRIMARY-deployment task exits 1"
    else
        fail "crashed PRIMARY-deployment task exits 1" "exit=$exit_code output=$output"
    fi

    if echo "$output" | grep -q "ERROR: CRASHED: 1 task(s) of deployment ecs-svc/new" \
        && echo "$output" | grep -q 'task/test-cluster/newtask: stoppedReason="Essential container in task exited"' \
        && echo "$output" | grep -q 'container ingestion-worker exitCode=1 reason="ModuleNotFoundError"'; then
        pass "crash report names the task, stoppedReason and exit code"
    else
        fail "crash report names the task, stoppedReason and exit code" "output=$output"
    fi

    if echo "$output" | grep -q "oldtask"; then
        fail "crash report leaves out the task the deployment replaced" "output=$output"
    else
        pass "crash report leaves out the task the deployment replaced"
    fi

    local polls
    polls=$(grep -c "^ecs describe-services" "$tmpdir/calls.log" || true)
    if [[ "$polls" -eq 1 ]]; then
        pass "crash fails on the first poll instead of waiting out the budget"
    else
        fail "crash fails on the first poll instead of waiting out the budget" "polls=$polls"
    fi
}

# Test 3d (#5102): ECS reports the PRIMARY deployment FAILED → exit 1.
test_rollout_failed_fails() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    write_describe_services "$tmpdir" 0 1 FAILED

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 1 ]] && echo "$output" | grep -q "rolloutState=FAILED for PRIMARY deployment ecs-svc/new"; then
        pass "rolloutState FAILED exits 1 with the deployment named"
    else
        fail "rolloutState FAILED exits 1 with the deployment named" "exit=$exit_code output=$output"
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

# Test 10: describe-services failure (aws CLI errors) exits 125: the
# service state is unknown (same convention as wait-for-rollout.sh).
test_describe_services_failure() {
    local tmpdir
    tmpdir=$(make_temp_dir)
    # Intentionally do not create describe-services.json — the mock will
    # exit 1 on the first call.

    local output exit_code
    exit_code=0
    output=$(run_script "$tmpdir" 2>&1) || exit_code=$?

    if [[ "$exit_code" -eq 125 ]]; then
        pass "describe-services failure exits 125 (state unknown)"
    else
        fail "describe-services failure exits 125 (state unknown)" "exit=$exit_code output=$output"
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
test_transient_zero_during_replacement_passes
test_crashed_task_fails_with_reason
test_rollout_failed_fails
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
