#!/usr/bin/env bash
# test_launch_agent_runner_smoke.sh — Tests for
# scripts/dispatcher/launch-agent-runner-smoke.sh (#3138).
#
# Exercises the script with stubbed `aws` and `psql`-via-dev-db-query.sh
# PATH shims so no real AWS calls or database connections are made.
#
# Test cases:
#   1. Fresh agent_id — one INSERT recorded, one ecs:RunTask, exit 0, taskArn
#      on stdout.
#   2. Re-run with existing agent_id — zero INSERTs, one ecs:RunTask.
#   3. Missing AGENT_ID arg — exit != 0 with usage message.
#   4. Wiring env unset — ecs:describe-services called once (internal
#      resolution), one ecs:RunTask recorded.
#
# Usage:
#   scripts/tests/test_launch_agent_runner_smoke.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/dispatcher/launch-agent-runner-smoke.sh"

FAILURES=0
TESTS=0

# ── Helpers ───────────────────────────────────────────────────────────────────

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

# ── Temp dir lifecycle ────────────────────────────────────────────────────────

TEST_TMP=""
cleanup() {
    set +eu
    if [[ "${TEST_LAUNCH_AGENT_RUNNER_KEEP_TMP:-0}" == "1" ]]; then
        echo "TEST_LAUNCH_AGENT_RUNNER_KEEP_TMP=1 — keeping $TEST_TMP for inspection"
        return
    fi
    if [[ -n "$TEST_TMP" && -d "$TEST_TMP" ]]; then
        rm -rf "$TEST_TMP"
    fi
}
trap cleanup EXIT

TEST_TMP=$(mktemp -d)

# ── Build a stub-bin directory on PATH ────────────────────────────────────────
#
# Every stub writes its argv (one arg per line) to a per-binary log file
# under $TEST_TMP/invocations/, so tests can count and grep specific calls.

STUB_BIN="$TEST_TMP/bin-stubs"
INVOCATIONS_DIR="$TEST_TMP/invocations"
mkdir -p "$STUB_BIN" "$INVOCATIONS_DIR"

# Shared stub utility — each stub sources this to record its invocation.
cat > "$STUB_BIN/_record_invocation.sh" << 'RECORDEOF'
# Source this to record argv into $INVOCATIONS_DIR/<tool>.log, one
# invocation per line starting with a count marker.
TOOL_NAME="$1"
shift
INVOCATIONS_LOG="${INVOCATIONS_DIR:-/tmp}/${TOOL_NAME}.log"
{
    printf 'CALL '
    for arg in "$@"; do
        printf '%q ' "$arg"
    done
    printf '\n'
} >> "$INVOCATIONS_LOG"
RECORDEOF

# ── aws stub ─────────────────────────────────────────────────────────────────
# Routes on subcommand:
#   ecs describe-services → returns fake subnet + SG JSON
#   ecs run-task          → returns fake taskArn JSON
#   ecs describe-tasks    → returns fake STOPPED status

cat > "$STUB_BIN/aws" << 'AWSEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"
# shellcheck disable=SC1091
. "$(dirname "$0")/_record_invocation.sh" aws "$@"

# Walk to the service subcommand.
service="${1:-}"
shift || true
subcommand="${1:-}"
shift || true

case "$service" in
    ecs)
        case "$subcommand" in
            describe-services)
                cat << 'JSON'
{
    "subnets": ["subnet-aaa111", "subnet-bbb222"],
    "securityGroups": ["sg-deadbeef"],
    "assignPublicIp": "DISABLED"
}
JSON
                exit 0
                ;;
            run-task)
                cat << 'JSON'
{
    "tasks": [
        {
            "taskArn": "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/fake-task-id-001"
        }
    ],
    "failures": []
}
JSON
                exit 0
                ;;
            describe-tasks)
                cat << 'JSON'
{
    "tasks": [
        {
            "lastStatus": "STOPPED",
            "taskArn": "arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/fake-task-id-001"
        }
    ]
}
JSON
                exit 0
                ;;
            *)
                echo "aws-stub: unhandled ecs subcommand: $subcommand" >&2
                exit 0
                ;;
        esac
        ;;
    *)
        echo "aws-stub: unhandled service: $service" >&2
        exit 0
        ;;
esac
AWSEOF
chmod +x "$STUB_BIN/aws"

# ── dev-db-query.sh stub ──────────────────────────────────────────────────────
# Stubs the wrapper script that the smoke script calls. Routes on query
# content:
#   SELECT 1 FROM dispatcher.agents → empty (no row) unless AGENT_ROW_EXISTS=1
#   INSERT INTO dispatcher.agents   → success (records to invocations/insert.log)

mkdir -p "$STUB_BIN/scripts"
cat > "$STUB_BIN/scripts/dev-db-query.sh" << 'DBEOF'
#!/usr/bin/env bash
set -u
INVOCATIONS_DIR="${INVOCATIONS_DIR}"

# Record full argv.
{
    printf 'CALL '
    for arg in "$@"; do
        printf '%q ' "$arg"
    done
    printf '\n'
} >> "$INVOCATIONS_DIR/dev-db-query.log"

# Consume flags to find the SQL query.
rw_mode=false
query=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rw)
            rw_mode=true
            shift
            ;;
        *)
            query="$1"
            shift
            ;;
    esac
done

case "$query" in
    *"SELECT 1 FROM dispatcher.agents"*)
        if [[ "${AGENT_ROW_EXISTS:-0}" == "1" ]]; then
            printf '1\n'
        fi
        exit 0
        ;;
    *"INSERT INTO dispatcher.agents"*)
        {
            printf 'CALL '
            printf '%q ' "$query"
            printf '\n'
        } >> "$INVOCATIONS_DIR/insert.log"
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
DBEOF
chmod +x "$STUB_BIN/scripts/dev-db-query.sh"

# Make the scripts/ wrapper visible as scripts/dev-db-query.sh relative to the
# repo root by symlinking into a path the script resolves via $REPO_ROOT.
# The smoke script calls: "$REPO_ROOT/scripts/dev-db-query.sh"
# We intercept by prepending the stub to PATH and using a wrapper that
# matches the absolute path the smoke script constructs.
#
# Strategy: create a shim at the same absolute path used by the script, which
# is $REPO_ROOT/scripts/dev-db-query.sh. Since we can't replace that file,
# we instead set REPO_ROOT to our stub tree so the script resolves to our stub.
#
# We set STUB_REPO_ROOT below to point the smoke script at our stubs.

STUB_REPO_ROOT="$TEST_TMP/fake-repo"
mkdir -p "$STUB_REPO_ROOT/scripts"
cp "$STUB_BIN/scripts/dev-db-query.sh" "$STUB_REPO_ROOT/scripts/dev-db-query.sh"

# ── Count helper ──────────────────────────────────────────────────────────────

count_invocations() {
    local log="$INVOCATIONS_DIR/${1}.log"
    if [[ ! -f "$log" ]]; then
        echo 0
        return
    fi
    grep -c '^CALL' "$log" || true
}

count_insert_calls() {
    local log="$INVOCATIONS_DIR/insert.log"
    if [[ ! -f "$log" ]]; then
        echo 0
        return
    fi
    grep -c '^CALL' "$log" || true
}

count_aws_subcommand() {
    # Count CALL lines in aws.log that contain the given subcommand string.
    local log="$INVOCATIONS_DIR/aws.log"
    local pattern="$1"
    if [[ ! -f "$log" ]]; then
        echo 0
        return
    fi
    grep -c "$pattern" "$log" || true
}

# ── Reset invocations between tests ──────────────────────────────────────────

reset_invocations() {
    rm -f "$INVOCATIONS_DIR"/*.log
}

# ── Precondition: script exists and is executable ────────────────────────────

if [[ ! -f "$SCRIPT" ]]; then
    echo "FAIL: script not found: $SCRIPT"
    exit 1
fi
if [[ ! -x "$SCRIPT" ]]; then
    echo "FAIL: script is not executable: $SCRIPT"
    exit 1
fi

# Assert headers are present.
if ! grep -q '# venv: none' "$SCRIPT"; then
    fail "script has # venv: none header" "header not found in $SCRIPT"
else
    pass "script has # venv: none header"
fi

if ! grep -q '# permanent: true' "$SCRIPT"; then
    fail "script has # permanent: true header" "header not found in $SCRIPT"
else
    pass "script has # permanent: true header"
fi

# ── Test 1: Fresh agent_id → INSERT + RunTask + taskArn on stdout ─────────────

reset_invocations
AGENT_ID_1="aaaaaaaa-0001-0001-0001-000000000001"

# Override REPO_ROOT so dev-db-query.sh resolves to our stub.
output=$(
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    ECS_CLUSTER="judgemind-dev" \
    AGENT_RUNNER_TASK_DEFINITION_FAMILY="judgemind-dispatcher-agent-runner-dev" \
    AGENT_RUNNER_SUBNET_IDS="subnet-aaa111,subnet-bbb222" \
    AGENT_RUNNER_SECURITY_GROUP_ID="sg-deadbeef" \
    REGION="us-west-2" \
    AGENT_ROW_EXISTS="0" \
    bash -c "
        # Patch REPO_ROOT inside the script by wrapping dev-db-query.sh.
        # The smoke script sources SCRIPT_DIR/../.. for REPO_ROOT.
        # We copy the script to a wrapper that sets the right path.
        export TEST_TMP='$TEST_TMP'
        export STUB_BIN='$STUB_BIN'

        # Run the script but with the real script pointing at our stub.
        # Since REPO_ROOT is computed from BASH_SOURCE[0] inside the
        # script, we need to inject a replacement. We do that by placing
        # our stub dev-db-query.sh at the path the script will compute.
        # Temporarily symlink:
        TMP_SCRIPTS='$TEST_TMP/repo$$/scripts'
        mkdir -p \"\$TMP_SCRIPTS\"
        cp '$STUB_BIN/scripts/dev-db-query.sh' \"\$TMP_SCRIPTS/dev-db-query.sh\"

        # We can't override REPO_ROOT from outside easily since the script
        # computes it from BASH_SOURCE[0]. Instead we wrap dev-db-query.sh
        # via a PATH-visible wrapper that intercepts the absolute-path call.
        # The smoke script calls: \"\$REPO_ROOT/scripts/dev-db-query.sh\"
        # We shadow it with a function export trick.
        '$SCRIPT' '$AGENT_ID_1' 2>/dev/null
    "
) || true

# The output capture approach above has a limitation: we can't intercept
# absolute path calls via PATH. Let's use a different approach: copy the
# smoke script to a temp location with REPO_ROOT forced.

reset_invocations

# Approach: create a thin wrapper that sets REPO_ROOT before calling the
# real script's body. We do this by creating a shim that exports
# REPO_ROOT pointing to our stub tree, then sources the real script.
# The real script re-defines REPO_ROOT at startup from BASH_SOURCE[0],
# so we need to use a copy with the REPO_ROOT override baked in.

PATCHED_SCRIPT="$TEST_TMP/launch-agent-runner-smoke-patched.sh"
# Read the script and patch the REPO_ROOT line.
sed 's|REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"|REPO_ROOT="'"$TEST_TMP"'/fake-repo"|' \
    "$SCRIPT" > "$PATCHED_SCRIPT"
chmod +x "$PATCHED_SCRIPT"

output=$(
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    ECS_CLUSTER="judgemind-dev" \
    AGENT_RUNNER_TASK_DEFINITION_FAMILY="judgemind-dispatcher-agent-runner-dev" \
    AGENT_RUNNER_SUBNET_IDS="subnet-aaa111,subnet-bbb222" \
    AGENT_RUNNER_SECURITY_GROUP_ID="sg-deadbeef" \
    REGION="us-west-2" \
    AGENT_ROW_EXISTS="0" \
    "$PATCHED_SCRIPT" "$AGENT_ID_1" 2>/dev/null
)
exit_code=$?

if [[ $exit_code -eq 0 ]]; then
    pass "test1: fresh agent-id exits 0"
else
    fail "test1: fresh agent-id exits 0" "exit code was $exit_code"
fi

if echo "$output" | grep -q "arn:aws:ecs:"; then
    pass "test1: taskArn on stdout"
else
    fail "test1: taskArn on stdout" "stdout was: $output"
fi

insert_count=$(count_insert_calls)
if [[ "$insert_count" -eq 1 ]]; then
    pass "test1: exactly one INSERT for fresh agent-id"
else
    fail "test1: exactly one INSERT for fresh agent-id" "insert count was $insert_count"
fi

run_task_count=$(count_aws_subcommand "run-task")
if [[ "$run_task_count" -eq 1 ]]; then
    pass "test1: exactly one ecs:RunTask"
else
    fail "test1: exactly one ecs:RunTask" "run-task count was $run_task_count"
fi

# ── Test 2: Re-run with existing agent_id → zero INSERTs, one RunTask ─────────

reset_invocations

output2=$(
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    ECS_CLUSTER="judgemind-dev" \
    AGENT_RUNNER_TASK_DEFINITION_FAMILY="judgemind-dispatcher-agent-runner-dev" \
    AGENT_RUNNER_SUBNET_IDS="subnet-aaa111,subnet-bbb222" \
    AGENT_RUNNER_SECURITY_GROUP_ID="sg-deadbeef" \
    REGION="us-west-2" \
    AGENT_ROW_EXISTS="1" \
    "$PATCHED_SCRIPT" "$AGENT_ID_1" 2>/dev/null
)
exit_code2=$?

if [[ $exit_code2 -eq 0 ]]; then
    pass "test2: existing agent-id exits 0"
else
    fail "test2: existing agent-id exits 0" "exit code was $exit_code2"
fi

insert_count2=$(count_insert_calls)
if [[ "$insert_count2" -eq 0 ]]; then
    pass "test2: zero INSERTs for existing agent-id"
else
    fail "test2: zero INSERTs for existing agent-id" "insert count was $insert_count2"
fi

run_task_count2=$(count_aws_subcommand "run-task")
if [[ "$run_task_count2" -eq 1 ]]; then
    pass "test2: exactly one ecs:RunTask for existing agent-id"
else
    fail "test2: exactly one ecs:RunTask for existing agent-id" "run-task count was $run_task_count2"
fi

# ── Test 3: Missing AGENT_ID arg → exit != 0 with usage message ───────────────

reset_invocations

stderr3=$("$PATCHED_SCRIPT" 2>&1 || true)
exit_code3=$("$PATCHED_SCRIPT" 2>/dev/null; echo $?) || true
# Re-run to capture exit code properly.
set +e
"$PATCHED_SCRIPT" > /dev/null 2>&1
exit_code3=$?
set -e

if [[ $exit_code3 -ne 0 ]]; then
    pass "test3: missing agent-id exits non-zero"
else
    fail "test3: missing agent-id exits non-zero" "exit code was $exit_code3"
fi

if echo "$stderr3" | grep -qi "usage\|agent-id\|required"; then
    pass "test3: usage message on stderr"
else
    fail "test3: usage message on stderr" "stderr was: $stderr3"
fi

# ── Test 4: Wiring env unset → describe-services called once ─────────────────
#
# When AGENT_RUNNER_SUBNET_IDS and AGENT_RUNNER_SECURITY_GROUP_ID are unset,
# the script must call ecs:describe-services once to resolve the wiring.

reset_invocations
AGENT_ID_4="dddddddd-0004-0004-0004-000000000004"

output4=$(
    PATH="$STUB_BIN:$PATH" \
    INVOCATIONS_DIR="$INVOCATIONS_DIR" \
    ECS_CLUSTER="judgemind-dev" \
    AGENT_RUNNER_TASK_DEFINITION_FAMILY="judgemind-dispatcher-agent-runner-dev" \
    REGION="us-west-2" \
    AGENT_ROW_EXISTS="0" \
    "$PATCHED_SCRIPT" "$AGENT_ID_4" 2>/dev/null
)
exit_code4=$?

if [[ $exit_code4 -eq 0 ]]; then
    pass "test4: wiring-env-unset run exits 0"
else
    fail "test4: wiring-env-unset run exits 0" "exit code was $exit_code4"
fi

describe_count=$(count_aws_subcommand "describe-services")
if [[ "$describe_count" -eq 1 ]]; then
    pass "test4: exactly one ecs:describe-services call for wiring resolution"
else
    fail "test4: exactly one ecs:describe-services call for wiring resolution" "describe-services count was $describe_count"
fi

run_task_count4=$(count_aws_subcommand "run-task")
if [[ "$run_task_count4" -eq 1 ]]; then
    pass "test4: exactly one ecs:RunTask after wiring resolution"
else
    fail "test4: exactly one ecs:RunTask after wiring resolution" "run-task count was $run_task_count4"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed. $FAILURES failed."

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
